"""
cleanup_orphaned_venues_and_events.py — One-time backfill (2026-08-29).

Venue and Event never had automatic pruning: update_performance's field loop
would repoint venue_id/event_id without ever checking whether the row it left
behind was still referenced anywhere, so orphans just accumulated silently.
prune_venue_if_orphaned / prune_event_if_orphaned (added this session to
app/utils/pruning.py) now run inline on every edit going forward — this
script clears the backlog that built up before that existed.

Reuses the real prune functions rather than reimplementing the orphan check,
so this script can never disagree with what the app itself considers
orphaned — single source of truth, per pruning.py's own docstring.

A Venue only counts as orphaned with BOTH zero Performances AND zero Events
anchored to it (an Event's venue_id is a separate reference). Any venue
photos are unlinked from disk only after the delete actually commits — see
prune_venue_if_orphaned's docstring for why that ordering matters.

Approved by Ryan (2026-08-29): also fine to re-run after the festival Event
merge (Rockygrass/Telluride/Merlefest/etc.) to sweep whatever it orphans.
Idempotent — running it again with nothing new orphaned reports 0 and exits
clean.

Run:
    python3 scripts/cleanup_orphaned_venues_and_events.py --dry-run
    python3 scripts/cleanup_orphaned_venues_and_events.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
from app.models.venue import Venue
from app.models.event import Event
from app.models.performance import Performance
from app.utils.pruning import prune_venue_if_orphaned, prune_event_if_orphaned


def find_orphaned_venue_ids():
    return [
        v.id for v in db.session.query(Venue).all()
        if db.session.query(Performance).filter_by(venue_id=v.id).count() == 0
        and db.session.query(Event).filter_by(venue_id=v.id).count() == 0
    ]


def find_orphaned_event_ids():
    return [
        e.id for e in db.session.query(Event).all()
        if db.session.query(Performance).filter_by(event_id=e.id).count() == 0
    ]


def main():
    dry = "--dry-run" in sys.argv
    app = create_app()
    with app.app_context():
        venue_ids = find_orphaned_venue_ids()
        event_ids = find_orphaned_event_ids()

        print(f"{'DRY RUN — ' if dry else ''}Orphaned Venue rows: {len(venue_ids)}")
        for v in db.session.query(Venue).filter(Venue.id.in_(venue_ids)).order_by(Venue.name):
            n_images = len(v.images)
            print(f"    id {v.id:>4}  {v.name!r}"
                  + (f"  [{n_images} photo(s)]" if n_images else ""))

        print(f"\n{'DRY RUN — ' if dry else ''}Orphaned Event rows: {len(event_ids)}")
        for e in db.session.query(Event).filter(Event.id.in_(event_ids)).order_by(Event.name):
            print(f"    id {e.id:>4}  {e.name!r}")

        if dry:
            print("\nDry run only — no changes made.")
            return

        pending_image_cleanup = []
        deleted_venues = deleted_events = 0
        for vid in venue_ids:
            ids, image_paths = prune_venue_if_orphaned(vid)
            deleted_venues += len(ids)
            pending_image_cleanup += image_paths
        for eid in event_ids:
            ids = prune_event_if_orphaned(eid)
            deleted_events += len(ids)

        db.session.commit()

        for path in pending_image_cleanup:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

        print(f"\nDone. Deleted {deleted_venues} venue row(s), {deleted_events} event row(s), "
              f"unlinked {len(pending_image_cleanup)} photo file(s).")
        print("integrity:", db.session.execute(db.text("PRAGMA integrity_check")).scalar())


if __name__ == "__main__":
    main()
