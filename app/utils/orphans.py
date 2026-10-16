"""app/utils/orphans.py -- clean up what a recording left behind.

A queue row creates its Artist, Venue, Event and Engineers when the folder is read, so
a row that is cleared or trashed leaves them behind. This removes the ones nobody
confirmed or enriched, and only those. When unsure, an entity stays.

Untouched means: no recordings or shows left on it, and nothing a person added: no
photos, bio, notes, history, aliases, MusicBrainz link, members, genre, research run,
and (for an Artist) never confirmed. Genres are never removed.

Callers own the transaction (nothing here commits).
"""
from app.extensions import db

UNKNOWN_ARTIST_NAME = "Unknown Artist"      # the stand-in act of a queue row whose folder names none


def collect_ids(rec):
    """The entity ids a recording points at, taken BEFORE the row changes or goes."""
    ids = {"performance": None, "artist": None, "venue": None, "event": None,
           "engineers": [], "musicians": []}
    if rec is None:
        return ids
    perf = rec.performance
    if perf is not None:
        ids["performance"] = perf.id
        ids["artist"], ids["venue"], ids["event"] = perf.artist_id, perf.venue_id, perf.event_id
        ids["musicians"] = [pp.musician_id for pp in perf.personnel]
    ids["engineers"] = [i for i in (rec.recording_engineer_id, rec.transfer_engineer_id,
                                    rec.mastering_engineer_id) if i]
    return ids


def _has_run(subject_type, subject_id):
    from app.models.lomax import LomaxRun
    return db.session.query(LomaxRun.id).filter(
        LomaxRun.subject_type == subject_type, LomaxRun.subject_id == subject_id).first() is not None


def _untouched_artist(a):
    from app.models.alias import ArtistAlias
    from app.models.musician import Membership
    from app.models.performance import Performance
    if a.confirmed_at is not None:
        return False
    if any((a.bio, a.abbreviation, a.image_ext, a.dossier_json, a.lineup_json, a.mbid,
            a.mb_status, a.genre_id, a.mb_links_json, a.mb_extra_json, a.mb_checked_at)):
        return False
    if a.images or a.resources:
        return False
    q = db.session.query
    if q(ArtistAlias.id).filter(ArtistAlias.artist_id == a.id).first():
        return False
    if q(Membership.id).filter(Membership.artist_id == a.id).first():
        return False
    if q(Performance.id).filter(Performance.artist_id == a.id).first():
        return False
    return not _has_run("artist", a.id)


def _untouched_venue(v):
    from app.models.alias import VenueAlias
    from app.models.event import Event
    from app.models.performance import Performance
    if v.bio or v.history or v.images:
        return False
    q = db.session.query
    if q(VenueAlias.id).filter(VenueAlias.venue_id == v.id).first():
        return False
    if q(Performance.id).filter(Performance.venue_id == v.id).first():
        return False
    if q(Event.id).filter(Event.venue_id == v.id).first():
        return False
    return not _has_run("venue", v.id)


def _untouched_event(e):
    from app.models.performance import Performance
    if e.notes or e.history or e.images or e.venue_id:
        return False
    if any((e.start_year, e.start_month, e.start_day, e.end_year, e.end_month, e.end_day)):
        return False
    return db.session.query(Performance.id).filter(Performance.event_id == e.id).first() is None


def _untouched_engineer(g):
    from app.models.alias import EngineerAlias
    from app.models.recording import Recording
    if g.bio or g.images:
        return False
    q = db.session.query
    if q(EngineerAlias.id).filter(EngineerAlias.engineer_id == g.id).first():
        return False
    return q(Recording.id).filter(
        (Recording.recording_engineer_id == g.id) | (Recording.transfer_engineer_id == g.id)
        | (Recording.mastering_engineer_id == g.id)).first() is None


def _untouched_musician(m):
    from app.models.musician import Membership
    from app.models.performance_personnel import PerformancePersonnel
    if m.bio or m.images:
        return False
    q = db.session.query
    return (q(Membership.id).filter(Membership.musician_id == m.id).first() is None
            and q(PerformancePersonnel.id).filter(PerformancePersonnel.musician_id == m.id).first() is None)


def cleanup(ids):
    """Remove the performance and the entities in `ids` (collect_ids) that are left
    with nothing and were never confirmed or enriched. Returns the counts removed."""
    from app.models.artist import Artist
    from app.models.engineer import Engineer
    from app.models.event import Event
    from app.models.musician import Musician, Membership
    from app.models.performance import Performance
    from app.models.recording import Recording
    from app.models.user import UserArtistPermission
    from app.models.venue import Venue
    out = {"performances": 0, "artists": 0, "venues": 0, "events": 0, "engineers": 0, "musicians": 0}
    musicians = set(ids.get("musicians") or [])

    pid = ids.get("performance")
    perf = db.session.get(Performance, pid) if pid else None
    if perf is not None and db.session.query(Recording.id).filter(
            Recording.performance_id == perf.id).first() is None:
        musicians.update(pp.musician_id for pp in perf.personnel)
        db.session.delete(perf)             # its personnel rows cascade
        db.session.flush()
        out["performances"] = 1

    aid = ids.get("artist")
    a = db.session.get(Artist, aid) if aid else None
    if a is not None and _untouched_artist(a):
        musicians.update(m.musician_id for m in
                         db.session.query(Membership).filter(Membership.artist_id == a.id))
        db.session.query(UserArtistPermission).filter_by(artist_id=a.id).delete(synchronize_session=False)
        db.session.delete(a)
        db.session.flush()
        out["artists"] = 1

    eid = ids.get("event")
    e = db.session.get(Event, eid) if eid else None
    if e is not None and _untouched_event(e):
        db.session.delete(e)
        db.session.flush()
        out["events"] = 1

    vid = ids.get("venue")
    v = db.session.get(Venue, vid) if vid else None
    if v is not None and _untouched_venue(v):
        db.session.delete(v)
        db.session.flush()
        out["venues"] = 1

    for gid in set(ids.get("engineers") or []):
        g = db.session.get(Engineer, gid)
        if g is not None and _untouched_engineer(g):
            db.session.delete(g)
            db.session.flush()
            out["engineers"] += 1

    for mid in musicians:
        m = db.session.get(Musician, mid)
        if m is not None and _untouched_musician(m):
            db.session.delete(m)
            db.session.flush()
            out["musicians"] += 1
    return out
