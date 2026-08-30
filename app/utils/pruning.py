"""
utils/pruning.py — Cascade cleanup of empty chain rows.

After a recording is deleted or a performance reassigned, prune the empty chain:
  performance with 0 recordings → performer with 0 performances → orphan Artists
  (people with no remaining memberships AND no remaining show-level personnel
  rows — see 2026-07-18 note below).

Single source of truth so the call sites (recordings.delete_recording,
performances.update_performance) stay consistent. SQLite FK enforcement is off,
so cascades are done here in app code, bottom-up.

(2026-07-11 remodel: Performer = the act; Artist = a person; Membership = M2M.)
(2026-07-18 Per-Show Personnel: an Artist can now also be referenced by a
PerformancePersonnel row with zero Memberships at all — a pure guest/sit-in,
or someone in an 'explicit'-mode act. Orphan checks must count both, or a
guest-only Artist gets pruned out from under the very show that references
them.)
"""

from app.extensions import db
from app.models.performance import Performance
from app.models.performer import Performer
from app.models.artist import Artist, Membership
from app.models.performance_personnel import PerformancePersonnel
from app.models.user import UserArtistPermission
from app.models.recording import Recording
from pathlib import Path
from flask import current_app
from app.models.venue import Venue
from app.models.event import Event
from app.utils.ingest import _sanitize_path


def _delete_orphan_artists(artist_ids):
    """Delete any Artist (person) left with 0 memberships AND 0 show-level
    personnel rows. Checking memberships alone would prune a guest-only
    sit-in (e.g. Branford Marsalis on one Dead show) right out from under
    the performance_personnel row that still references them."""
    deleted = []
    for aid in artist_ids:
        has_membership = db.session.query(Membership).filter_by(artist_id=aid).count() > 0
        has_personnel  = db.session.query(PerformancePersonnel).filter_by(artist_id=aid).count() > 0
        if not has_membership and not has_personnel:
            a = db.session.get(Artist, aid)
            if a:
                deleted.append(a.id)
                db.session.delete(a)
    db.session.flush()
    return deleted


def prune_performer_if_orphaned(performer_id):
    """
    If a Performer has no performances left, delete it (+ its memberships and
    permissions), then delete any member Artist left with 0 memberships.
    Returns {"performers": [...], "artists": [...]}.
    """
    result = {"performers": [], "artists": []}
    if db.session.query(Performance).filter_by(performer_id=performer_id).count() > 0:
        return result

    member_artist_ids = [
        m.artist_id
        for m in db.session.query(Membership).filter_by(performer_id=performer_id).all()
    ]
    db.session.query(UserArtistPermission).filter_by(performer_id=performer_id).delete(
        synchronize_session=False)
    performer = db.session.get(Performer, performer_id)
    if performer:
        result["performers"].append(performer.id)
        db.session.delete(performer)   # memberships cascade-delete
    db.session.flush()

    result["artists"] = _delete_orphan_artists(member_artist_ids)
    return result


def prune_after_recording_delete(performance_id):
    """
    After a recording is removed, prune the empty chain above it.
    Returns {"performances": [...], "performers": [...], "artists": [...]}.
    """
    pruned = {"performances": [], "performers": [], "artists": []}

    perf = db.session.get(Performance, performance_id)
    if not perf:
        return pruned
    if db.session.query(Recording).filter_by(performance_id=perf.id).count() > 0:
        return pruned

    performer_id = perf.performer_id
    # Capture show-level personnel artist ids before Performance's
    # cascade="all, delete-orphan" wipes their performance_personnel rows
    # below, so a pure guest (no Membership anywhere) can still be checked
    # for orphaning — prune_performer_if_orphaned only ever looks at the
    # act's Membership roster, so it would otherwise miss them entirely.
    personnel_artist_ids = [pp.artist_id for pp in perf.personnel]

    pruned["performances"].append(perf.id)
    db.session.delete(perf)
    db.session.flush()

    sub = prune_performer_if_orphaned(performer_id)
    pruned["performers"] = sub["performers"]
    pruned["artists"]    = sub["artists"]

    already_checked = set(sub["artists"])
    extra_candidates = [aid for aid in personnel_artist_ids if aid not in already_checked]
    if extra_candidates:
        pruned["artists"] += _delete_orphan_artists(extra_candidates)

    return pruned


def prune_venue_if_orphaned(venue_id):
    """
    If a Venue has no performances left AND no Events anchored to it, delete
    it. Mirrors prune_performer_if_orphaned's trip-wire; called from
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
    prune_performer_if_orphaned's trip-wire; called from
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
