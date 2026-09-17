"""
utils/pruning.py — Cascade cleanup of empty chain rows.

After a recording is deleted or a performance reassigned, prune the empty chain:
  performance with 0 recordings → artist with 0 performances → orphan Musicians
  (people with no remaining memberships AND no remaining show-level personnel
  rows — see 2026-07-18 note below).

Single source of truth so the call sites (recordings.delete_recording,
performances.update_performance) stay consistent. SQLite FK enforcement is off,
so cascades are done here in app code, bottom-up.

(Artist = the act; Musician = a person; Membership = M2M.)
(2026-07-18 Per-Show Personnel: a Musician can now also be referenced by a
PerformancePersonnel row with zero Memberships at all — a pure guest/sit-in,
or someone in an 'explicit'-mode act. Orphan checks must count both, or a
guest-only Musician gets pruned out from under the very show that references
them.)
"""

from app.extensions import db
from app.models.performance import Performance
from app.models.artist import Artist
from app.models.musician import Musician, Membership
from app.models.performance_personnel import PerformancePersonnel
from app.models.user import UserArtistPermission
from app.models.recording import Recording
from pathlib import Path
from flask import current_app
from app.models.venue import Venue
from app.models.event import Event
from app.utils.ingest import _sanitize_path


def _delete_orphan_musicians(musician_ids):
    """Delete any Musician (person) left with 0 memberships AND 0 show-level
    personnel rows. Checking memberships alone would prune a guest-only
    sit-in (e.g. Branford Marsalis on one Dead show) right out from under
    the performance_personnel row that still references them."""
    deleted = []
    for aid in musician_ids:
        has_membership = db.session.query(Membership).filter_by(musician_id=aid).count() > 0
        has_personnel  = db.session.query(PerformancePersonnel).filter_by(musician_id=aid).count() > 0
        if not has_membership and not has_personnel:
            a = db.session.get(Musician, aid)
            if a:
                deleted.append(a.id)
                db.session.delete(a)
    db.session.flush()
    return deleted


def prune_artist_if_orphaned(artist_id):
    """
    If an Artist has no performances left, delete it (+ its memberships and
    permissions), then delete any member Musician left with 0 memberships.
    Returns {"artists": [...], "musicians": [...]}.
    """
    result = {"artists": [], "musicians": []}
    if db.session.query(Performance).filter_by(artist_id=artist_id).count() > 0:
        return result

    member_musician_ids = [
        m.musician_id
        for m in db.session.query(Membership).filter_by(artist_id=artist_id).all()
    ]
    db.session.query(UserArtistPermission).filter_by(artist_id=artist_id).delete(
        synchronize_session=False)
    artist = db.session.get(Artist, artist_id)
    if artist:
        result["artists"].append(artist.id)
        db.session.delete(artist)   # memberships cascade-delete
    db.session.flush()

    result["musicians"] = _delete_orphan_musicians(member_musician_ids)
    return result


def prune_after_recording_delete(performance_id):
    """
    After a recording is removed, prune the empty chain above it.
    Returns {"performances": [...], "artists": [...], "musicians": [...]}.
    """
    pruned = {"performances": [], "artists": [], "musicians": []}

    perf = db.session.get(Performance, performance_id)
    if not perf:
        return pruned
    if db.session.query(Recording).filter_by(performance_id=perf.id).count() > 0:
        return pruned

    artist_id = perf.artist_id
    # Capture show-level personnel musician ids before Performance's
    # cascade="all, delete-orphan" wipes their performance_personnel rows
    # below, so a pure guest (no Membership anywhere) can still be checked
    # for orphaning — prune_artist_if_orphaned only ever looks at the
    # act's Membership roster, so it would otherwise miss them entirely.
    personnel_musician_ids = [pp.musician_id for pp in perf.personnel]

    pruned["performances"].append(perf.id)
    db.session.delete(perf)
    db.session.flush()

    sub = prune_artist_if_orphaned(artist_id)
    pruned["artists"] = sub["artists"]
    pruned["musicians"]    = sub["musicians"]

    already_checked = set(sub["musicians"])
    extra_candidates = [aid for aid in personnel_musician_ids if aid not in already_checked]
    if extra_candidates:
        pruned["musicians"] += _delete_orphan_musicians(extra_candidates)

    return pruned


def prune_venue_if_orphaned(venue_id):
    """
    If a Venue has no performances left AND no Events anchored to it, delete
    it. Mirrors prune_artist_if_orphaned's trip-wire; called from
    performances.update_performance right after venue_id changes.

    A Venue can be referenced two ways -- Performance.venue_id (where a show
    happened) and Event.venue_id (a festival's anchor grounds) -- both must
    be clear before deleting, or repointing a show away from a venue could
    silently orphan the venue_id on an Event that still names it.

    Any VenueImage rows cascade-delete with the row (Venue.images carries
    cascade="all, delete-orphan"), but that only removes the DB rows. This
    function does NOT commit or touch the filesystem -- callers own the
    transaction, same as every other function in this module. The image
    file paths are handed back instead, so the CALLER can best-effort
    unlink them only after ITS OWN commit() actually lands -- unlinking
    before that commit would delete a real file underneath a transaction
    that might still roll back, which is worse than the orphan-file problem
    this is meant to solve. Same non-fatal post-commit ordering
    entity_images.handle_delete uses, just pushed out one more frame.

    Returns (deleted_ids, image_paths) -- image_paths is a list of Path
    objects for the caller to unlink after its commit succeeds.
    """
    if venue_id is None:
        return [], []
    if db.session.query(Performance).filter_by(venue_id=venue_id).count() > 0:
        return [], []
    if db.session.query(Event).filter_by(venue_id=venue_id).count() > 0:
        return [], []
    venue = db.session.get(Venue, venue_id)
    if not venue:
        return [], []

    deleted_id = venue.id
    image_paths = []
    if venue.images:
        library_root = current_app.config.get("LIBRARY_ROOT", "")
        images_dir = Path(library_root) / "_venues" / _sanitize_path(venue.name) / "_images"
        image_paths = [images_dir / img.filename for img in venue.images]

    db.session.delete(venue)   # VenueImage rows cascade-delete here
    db.session.flush()

    return [deleted_id], image_paths


def prune_event_if_orphaned(event_id):
    """
    If an Event has no performances left, delete it. Mirrors
    prune_artist_if_orphaned's trip-wire; called from
    performances.update_performance right after event_id changes.

    Returns [event_id] if deleted, else [].
    """
    if event_id is None:
        return []
    if db.session.query(Performance).filter_by(event_id=event_id).count() > 0:
        return []
    event = db.session.get(Event, event_id)
    if not event:
        return []

    deleted_id = event.id
    db.session.delete(event)
    db.session.flush()
    return [deleted_id]
