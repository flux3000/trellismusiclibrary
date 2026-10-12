"""audit_placeholder_venues.py — Read-only report on placeholder-named venues.

(and similar placeholder names like "TBD", "N/A") get created once and then
reused by name-match across every recording that doesn't have a real venue yet.
Since Venue is meant to be one canonical physical place, this silently shares
one row across shows that are, in reality, all over the map — and any write to
city/state/country on that row (AI Assist, manual edit, the venue-picker's
lockLocation) clobbers whatever was there for every OTHER show parked on it.

This script makes NO changes. It just lists every placeholder-named Venue row
and every Performance/Recording currently pointing at it, so the user can see
the blast radius and decide what to do with each show's location data by hand.

Run:
    python3 scripts/audit_placeholder_venues.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
from app.models.venue import Venue
from app.models.performance import Performance
from app.models.performer import Performer
from app.models.recording import Recording

# Same list used by the app-side fix.
PLACEHOLDER_VENUE_NAMES = {"unknown venue", "unknown", "tbd", "n/a", "various"}


def main():
    app = create_app()
    with app.app_context():
        venues = (
            db.session.query(Venue)
            .filter(db.func.lower(Venue.name).in_(PLACEHOLDER_VENUE_NAMES))
            .all()
        )

        if not venues:
            print("No placeholder-named venues found. Nothing to report.")
            return

        for v in venues:
            perfs = (
                db.session.query(Performance)
                .filter(Performance.venue_id == v.id)
                .all()
            )
            print(f"\nVenue #{v.id} — {v.name!r}")
            print(f"  current stored location: city={v.city!r} state={v.state!r} country={v.country!r}")
            print(f"  {len(perfs)} performance(s) pointing at this row:")

            if not perfs:
                print("    (none — safe to leave or delete)")
                continue

            for p in perfs:
                performer = db.session.get(Performer, p.performer_id)
                date = "-".join(
                    str(x) for x in (p.start_year, p.start_month, p.start_day) if x
                ) or "unknown date"
                rec_count = (
                    db.session.query(Recording)
                    .filter(Recording.performance_id == p.id)
                    .count()
                )
                print(
                    f"    Performance #{p.id} — {performer.name if performer else '?'} "
                    f"@ {date} — perf-level fallback: city={p.city!r} state={p.state!r} "
                    f"country={p.country!r} — {rec_count} recording(s)"
                )

        print(
            "\nNo changes made. Each performance above needs a human call on its "
            "real location — re-run AI Assist per show, or fix manually, then "
            "either unlink from the placeholder venue or leave venue_id null "
            "once the placeholder-venue fix lands."
        )


if __name__ == "__main__":
    main()
