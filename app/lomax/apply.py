"""
Applying an accepted proposal. Each field goes through the same helpers and rules the pages
use when a person edits that field by hand (Performance PUT, Recording PUT, Venue PUT, the
roster and stint helpers), and the change counts as human-set: it is logged on the recording,
and a changed act is marked confirmed.
"""
import json

from sqlalchemy import func

from app.extensions import db
from app.lomax import filters
from app.lomax.evidence import split_iso
from app.models.lomax import LomaxProposal, LomaxRun
from app.models.track import Track


TRACK_COLUMNS = {"title": "title", "songwriter": "songwriter", "note": "notes"}


class ApplyError(Exception):
    """The proposal cannot be applied as written; the message is shown to the archivist."""


def apply_proposal(prop: LomaxProposal, run: LomaxRun, user_id=None):
    """Apply one accepted proposal. Folder subjects have nothing saved yet, so the page applies
    those; the decision is still recorded by the caller."""
    if run.skill == "album" and run.subject_type == "recording":
        _apply_album(prop, run, user_id)
        return
    handler = {"recording": _apply_recording, "artist": _apply_artist, "venue": _apply_venue}.get(run.subject_type)
    if handler is None:
        return
    handler(prop, run, user_id)


# ── recording ────────────────────────────────────────────────────────────────

def _apply_recording(prop, run, user_id):
    from app.models.event import Event
    from app.models.performance import Performance  # noqa: F401
    from app.models.recording import Recording
    from app.models.recording_event import RecordingEvent
    from app.models.track import Track  # noqa: F401
    from app.models.venue import Venue
    from app.utils.artists import mark_artist_confirmed, resolve_or_create_artist
    from app.utils.event_names import clean_event_name
    from app.utils.folder_naming import rename_recording_folder
    from app.utils.pruning import prune_artist_if_orphaned, prune_event_if_orphaned, prune_venue_if_orphaned
    from app.utils.venues import is_placeholder_venue_name
    from flask import current_app

    rec = db.session.get(Recording, run.subject_id)
    if rec is None:
        raise ApplyError("The recording no longer exists.")
    if prop.field.startswith("track."):
        _apply_track(prop, rec, run, user_id)
        return
    if prop.field == "genre":
        _apply_genre(prop, rec, run, user_id)
        return
    perf = rec.performance
    f, v = prop.field, (prop.proposed or "").strip()
    image_paths = []

    if f in ("source", "lineage"):
        setattr(rec, f, v)
    elif f == "artist":
        if not v:
            raise ApplyError("Empty artist.")
        if v.lower() != perf.artist.name.lower():
            old = perf.artist_id
            artist = resolve_or_create_artist(v)
            mark_artist_confirmed(artist)
            perf.artist_id = artist.id
            db.session.flush()
            prune_artist_if_orphaned(old)
    elif f == "date":
        ymd = split_iso(v)
        if not ymd:
            raise ApplyError("%r is not an ISO date." % v)
        perf.start_year, perf.start_month, perf.start_day = ymd
    elif f == "venue":
        if is_placeholder_venue_name(v):
            raise ApplyError("A placeholder name is not a venue.")
        venue = db.session.query(Venue).filter(func.lower(Venue.name) == v.lower()).first()
        if venue is None:
            venue = Venue(name=v)
            db.session.add(venue)
            db.session.flush()
        old = perf.venue_id
        perf.venue_id = venue.id
        db.session.flush()
        if old != venue.id:
            _, image_paths = prune_venue_if_orphaned(old)
    elif f == "event":
        from app.api.ingest import _find_event
        name = clean_event_name(v)
        if not name:
            raise ApplyError("Empty event.")
        event = _find_event(name)
        if event is None:
            event = Event(name=name)
            db.session.add(event)
            db.session.flush()
        old = perf.event_id
        perf.event_id = event.id
        db.session.flush()
        if old != event.id:
            prune_event_if_orphaned(old)
    elif f == "stage":
        perf.stage = v or None
    elif f in ("city", "state", "country"):
        if f == "state":
            ven = perf.venue if perf.venue and not is_placeholder_venue_name(perf.venue.name) else None
            v = filters.state_code(v, (ven.country if ven else perf.country))
        # Location lands on a real linked venue, else on the performance's own fallback fields
        # (a placeholder-named venue is shared across unrelated shows and never gets one).
        if perf.venue and not is_placeholder_venue_name(perf.venue.name):
            setattr(perf.venue, f, v or None)
        else:
            setattr(perf, f, v or None)
    else:
        raise ApplyError("Lomax cannot apply a %r proposal to a recording." % f)

    db.session.add(RecordingEvent(recording_id=rec.id, user_id=user_id or run.created_by,
                                  event_type="metadata_updated", note="Lomax proposal accepted: %s" % f))
    library_root = current_app.config.get("LIBRARY_ROOT", "")
    for r in perf.recordings:
        rename_recording_folder(r, library_root)    # gated on the file-handling setting inside
    db.session.commit()
    for path in image_paths:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


def _apply_album(prop, run, user_id):
    """An album run's proposals: title, year, notes and the track title/songwriter. An accepted
    notes text replaces the recording's notes (it is a human decision)."""
    from app.models.recording import Recording
    from app.models.recording_event import RecordingEvent
    from app.lomax.skills.album import NOTES_MAX_CHARS, _year
    from app.utils.folder_naming import rename_recording_folder
    from flask import current_app

    rec = db.session.get(Recording, run.subject_id)
    if rec is None:
        raise ApplyError("The recording no longer exists.")
    f, v = prop.field, (prop.proposed or "").strip()
    if f.startswith("track."):
        if f.split(".")[-1] not in ("title", "songwriter"):
            raise ApplyError("Lomax cannot apply a %r proposal to an album track." % f)
        _apply_track(prop, rec, run, user_id)
        return
    if f == "title":
        if not v:
            raise ApplyError("Empty title.")
        rec.title = v
    elif f == "year":
        if rec.kind != "studio":
            raise ApplyError("A year is applied only to a studio record.")
        year = _year(v)
        if not year or rec.performance is None:
            raise ApplyError("%r is not a year." % v)
        rec.performance.start_year = int(year)
    elif f == "notes":
        if not v:
            raise ApplyError("Empty notes.")
        rec.notes = v[:NOTES_MAX_CHARS]
    else:
        raise ApplyError("Lomax cannot apply a %r proposal to an album." % f)
    db.session.add(RecordingEvent(recording_id=rec.id, user_id=user_id or run.created_by,
                                  event_type="metadata_updated", note="Lomax proposal accepted: %s" % f))
    if f in ("title", "year"):
        rename_recording_folder(rec, current_app.config.get("LIBRARY_ROOT", ""))   # gated inside
    db.session.commit()


def _apply_genre(prop, rec, run, user_id):
    """The act's genre, through the one genre-apply path (musicbrainz.apply_genre_to_artist): fill
    when EMPTY, matching an existing Trellis genre or else creating the named one. A genre a person
    already set is never replaced, and an act has one genre."""
    from app.models.recording_event import RecordingEvent
    from app.utils import musicbrainz as mb
    from app.utils.artists import mark_artist_confirmed
    perf = rec.performance
    artist = perf.artist if perf else None
    if artist is None:
        raise ApplyError("This recording has no act.")
    name = (prop.proposed or "").strip()
    if not name:
        raise ApplyError("Empty genre.")
    if artist.genre_id is not None:
        raise ApplyError("This act already has a genre.")
    mb.apply_genre_to_artist(artist, [name])
    mark_artist_confirmed(artist)
    db.session.add(RecordingEvent(recording_id=rec.id, user_id=user_id or run.created_by,
                                  event_type="metadata_updated", note="Lomax proposal accepted: genre"))
    db.session.commit()


def _apply_track(prop, rec, run, user_id):
    """track.<n>.title | songwriter | note: written straight to the Track row. The files on disk
    are not renamed; the page's own track edit does that."""
    from app.models.recording_event import RecordingEvent
    parts = prop.field.split(".")
    if len(parts) != 3 or parts[2] not in TRACK_COLUMNS or not parts[1].isdigit():
        raise ApplyError("Lomax cannot apply a %r proposal to a track." % prop.field)
    track = db.session.query(Track).filter_by(recording_id=rec.id, track_number=int(parts[1])).first()
    if track is None:
        raise ApplyError("Track %s no longer exists." % parts[1])
    v = (prop.proposed or "").strip()
    column = TRACK_COLUMNS[parts[2]]
    if column == "title" and not v:
        raise ApplyError("Empty title.")
    setattr(track, column, v if column == "title" else (v or None))
    db.session.add(RecordingEvent(recording_id=rec.id, user_id=user_id or run.created_by,
                                  event_type="metadata_updated", note="Lomax proposal accepted: %s" % prop.field))
    db.session.commit()


# ── artist ───────────────────────────────────────────────────────────────────

def _apply_artist(prop, run, user_id):
    from app.models.artist import Artist, ArtistResource
    from app.utils.artists import _is_unbounded, add_membership_stint, mark_artist_confirmed

    artist = db.session.get(Artist, run.subject_id)
    if artist is None:
        raise ApplyError("The artist no longer exists.")
    try:
        entry = json.loads(prop.proposed)
    except (ValueError, TypeError):
        raise ApplyError("Unreadable proposal.")

    if prop.field == "resource":
        url = (entry.get("url") or "").strip()
        if not url:
            raise ApplyError("No url.")
        if any(r.url == url for r in artist.resources):
            return
        nxt = max([r.order or 0 for r in artist.resources] + [-1]) + 1
        db.session.add(ArtistResource(artist_id=artist.id, label=(entry.get("label") or "").strip() or None,
                                      url=url, order=nxt))
    elif prop.field == "member":
        name = (entry.get("name") or "").strip()
        if not name:
            raise ApplyError("No name.")
        s, e = split_iso(entry.get("start")), split_iso(entry.get("end"))
        if (entry.get("start") and not s) or (entry.get("end") and not e):
            raise ApplyError("A date is not ISO.")
        s, e = s or (None, None, None), e or (None, None, None)
        stints = [m for m in artist.memberships if m.musician.name.lower() == name.lower()]
        if any((m.start_year, m.start_month, m.start_day, m.end_year, m.end_month, m.end_day) == (*s, *e)
               for m in stints):
            return                                          # already on the roster with these dates
        blank = next((m for m in stints if _is_unbounded(m)), None)
        if blank is not None:                               # fill the "always a member" row in place
            blank.start_year, blank.start_month, blank.start_day = s
            blank.end_year, blank.end_month, blank.end_day = e
            if not blank.instrument:
                blank.instrument = (entry.get("instrument") or "").strip() or None
        else:
            m = add_membership_stint(artist, name, start_year=s[0], start_month=s[1], start_day=s[2],
                                     end_year=e[0], end_month=e[1], end_day=e[2])
            m.instrument = (entry.get("instrument") or "").strip() or None
    else:
        raise ApplyError("Lomax cannot apply a %r proposal to an artist." % prop.field)
    mark_artist_confirmed(artist)
    db.session.commit()


# ── venue ────────────────────────────────────────────────────────────────────

def _apply_venue(prop, run, user_id):
    from app.models.venue import Venue
    venue = db.session.get(Venue, run.subject_id)
    if venue is None:
        raise ApplyError("The venue no longer exists.")
    if prop.field == "former_name":
        _add_venue_alias(venue, prop)
        return
    if prop.field not in ("city", "state", "country"):
        raise ApplyError("Lomax cannot apply a %r proposal to a venue." % prop.field)
    v = (prop.proposed or "").strip()
    setattr(venue, prop.field, (filters.state_code(v, venue.country) if prop.field == "state" else v) or None)
    db.session.commit()


def _add_venue_alias(venue, prop):
    """A former name becomes an alias of the venue, which the library index reads from then on."""
    from app.models.alias import VenueAlias
    from app.utils.reader.library import invalidate_library_cache, norm_key
    try:
        name = (json.loads(prop.proposed).get("name") or "").strip()
    except (ValueError, TypeError, AttributeError):
        raise ApplyError("Unreadable proposal.")
    key = norm_key(name)
    if not name or not key:
        raise ApplyError("No name.")
    if db.session.query(VenueAlias.id).filter_by(venue_id=venue.id, alias_key=key).first():
        return                                              # already an alias of this venue
    if norm_key(venue.name) == key:
        return                                              # it is the venue's own name
    other = db.session.query(VenueAlias.id).filter(VenueAlias.venue_id != venue.id, VenueAlias.alias_key == key).first()
    if other is not None:
        raise ApplyError("That name is already an alias of another venue.")
    db.session.add(VenueAlias(venue_id=venue.id, alias=name[:255], alias_key=key[:255]))
    db.session.commit()
    invalidate_library_cache()
