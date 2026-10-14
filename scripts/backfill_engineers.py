"""Fill Recording / Transfer / Mastering Engineer on recordings already in the
library, from their tags and stored info files. Only empty roles are filled.

    python3 scripts/backfill_engineers.py              # preview (writes nothing)
    python3 scripts/backfill_engineers.py --apply      # save
    python3 scripts/backfill_engineers.py --no-tags    # info files only (no file reads)
    python3 scripts/backfill_engineers.py --csv out.csv

Run from the repo root with the venv active, with Trellis closed.
The logic lives in app/utils/engineer_backfill.py.
"""
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.utils.engineer_backfill import backfill_engineers


def main():
    args = sys.argv[1:]
    apply = "--apply" in args
    read_tags = "--no-tags" not in args
    out_csv = args[args.index("--csv") + 1] if "--csv" in args else None
    app = create_app()
    with app.app_context():
        rows = backfill_engineers(apply=apply, read_tags=read_tags)
    for r in rows:
        print(f"  #{r['id']:<6} {r['role']:<10} {r['name']:<28} ({r['source']})  {r['folder']}")
    by_role = Counter(r["role"] for r in rows)
    recs = len({r["id"] for r in rows})
    print(f"\n{'Saved' if apply else '[preview] Would save'} {len(rows)} credit(s) on {recs} recording(s): "
          + ", ".join(f"{k} {v}" for k, v in sorted(by_role.items())))
    if out_csv:
        with open(out_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["id", "role", "name", "source", "folder"])
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {out_csv}")


if __name__ == "__main__":
    main()
