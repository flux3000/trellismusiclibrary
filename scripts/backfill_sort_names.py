"""backfill_sort_names.py — Apply approved Last,First sort names to existing artists.

  1. Clean "First Last" -> "Last, First"  (solo performers)
  2. "The X"            -> "X, The"        (drop leading article for sort)
  3. Bands / collectives / ambiguous joint-billing acts -> leave sort_name NULL
     (they already sort correctly on their raw name)

Only sets sort_name where it is currently NULL — never clobbers an existing
value (e.g. Bill Evans already has "Evans, Bill").

Run:
    python3 scripts/backfill_sort_names.py --dry-run   # preview
    python3 scripts/backfill_sort_names.py             # apply
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
from app.models.artist import Artist

# Explicit, reviewed mapping. Anything not listed here is left untouched.
SORT_NAMES = {
    "Al Green":          "Green, Al",
    "Bela Fleck":        "Fleck, Bela",
    "Bonnie Raitt":      "Raitt, Bonnie",
    "Brian Wilson":      "Wilson, Brian",
    "Bruce Hornsby":     "Hornsby, Bruce",
    "Bruce Springsteen": "Springsteen, Bruce",
    "Curtis Mayfield":   "Mayfield, Curtis",
    "Sonny Rollins":     "Rollins, Sonny",
    "The Byrds":         "Byrds, The",
    "The Meters":        "Meters, The",
}


def main():
    dry_run = "--dry-run" in sys.argv
    app = create_app()
    with app.app_context():
        changed = 0
        for name, sort_name in SORT_NAMES.items():
            a = db.session.query(Artist).filter_by(name=name).first()
            if not a:
                print(f"  SKIP — no artist named {name!r}")
                continue
            if a.sort_name:
                print(f"  SKIP — {name!r} already has sort_name {a.sort_name!r}")
                continue
            print(f"  {name!r:22} -> sort_name {sort_name!r}")
            if not dry_run:
                a.sort_name = sort_name
            changed += 1

        print(f"\n{'[dry-run] Would set' if dry_run else 'Set'} sort_name on {changed} artist(s).")
        if dry_run:
            db.session.rollback()
        else:
            db.session.commit()
            print("Committed.")


if __name__ == "__main__":
    main()
