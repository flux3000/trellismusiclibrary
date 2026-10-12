"""scripts/backfill_musicbrainz.py — MusicBrainz lookup for existing performers.

New Performers get matched at creation. The 164 that already exist have
`mb_status = NULL` ("never looked up"), so this walks them once.

Deliberately a separate script from the migration: this makes ~2 network calls
per performer at MusicBrainz's published 1 req/sec limit, so 164 acts is
roughly 6 minutes of wall clock. A schema migration must never quietly do that.

Resumable — only rows with `mb_status IS NULL` are considered, and each commits
on its own, so a Ctrl-C loses nothing and a re-run continues where it stopped.
Ambiguous results are recorded as 'ambiguous' and left for a human to resolve
on the performer page; nothing is ever auto-picked.

    python3 scripts/backfill_musicbrainz.py --dry-run
    python3 scripts/backfill_musicbrainz.py --limit 10
    python3 scripts/backfill_musicbrainz.py
    python3 scripts/backfill_musicbrainz.py --retry-none   # re-check 'none' rows
"""

import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
from app.models.performer import Performer
from app.utils import musicbrainz as mb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--retry-none", action="store_true",
                    help="also re-check performers previously resolved to 'none'")
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        q = db.session.query(Performer)
        if args.retry_none:
            q = q.filter(Performer.mb_status.is_(None) |
                         (Performer.mb_status == "none"))
        else:
            q = q.filter(Performer.mb_status.is_(None))
        todo = q.order_by(Performer.name).all()
        if args.limit:
            todo = todo[:args.limit]

        print(f"\nPerformers to look up: {len(todo)}")
        print(f"Mode: {'DRY RUN' if args.dry_run else 'write'}")
        print(f"Estimated time: ~{len(todo) * 2.2 / 60:.1f} min "
              f"(MusicBrainz allows 1 request/sec)\n")

        if args.dry_run:
            for p in todo[:40]:
                print(f"  would look up: {p.name}")
            if len(todo) > 40:
                print(f"  … and {len(todo) - 40} more")
            return 0

        tally = {"matched": 0, "ambiguous": 0, "none": 0}
        started = time.time()
        for n, p in enumerate(todo, 1):
            status = mb.try_match_performer(p)
            db.session.commit()      # per performer — Ctrl-C loses nothing
            tally[status] = tally.get(status, 0) + 1
            mark = {"matched": "✓", "ambiguous": "?", "none": "·"}.get(status, "·")
            extra = ""
            if status == "matched":
                extra = f"  {p.mb_type or ''} {('· ' + p.mb_area) if p.mb_area else ''}".rstrip()
            print(f"  [{n}/{len(todo)}] {mark} {p.name}{extra}")

        print(f"\n  matched {tally['matched']} · "
              f"needs resolving {tally['ambiguous']} · "
              f"no match {tally['none']}")
        print(f"  {(time.time() - started) / 60:.1f} min")
        print("\n  Ambiguous ones show a Match prompt on their performer page.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
