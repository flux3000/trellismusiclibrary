"""rescore_quality.py — re-score stored quality rows from cached features.

No audio decode. Use after any change to quality_scoring.py (weights, curves,
calibration constants, source offsets) so existing rows stop displaying numbers
from the previous engine.

    python3 scripts/rescore_quality.py --dry-run    # report only
    python3 scripts/rescore_quality.py              # write

Rows whose `analysis_version` is behind the current extractor are reported as
`stale_features` and SKIPPED — they predate a feature the new scoring reads, so
they need a real re-analysis (re-run Analyze in the app), not a rescore.

Rows written before that date are analysis v1, so they will show up as
stale_features and must be re-analysed.
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.utils import quality_store as qs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        r = qs.rescore_stored(dry_run=args.dry_run)

        print(f"{'DRY RUN — ' if args.dry_run else ''}"
              f"rescored {r['rescored']}  "
              f"stale_features {r['stale_features']}  "
              f"no_features {r['no_features']}")

        if r["changed"]:
            print(f"\n  {len(r['changed'])} row(s) changed:")
            for c in sorted(r["changed"],
                            key=lambda c: -abs(c["after"] - c["before"]))[:25]:
                delta = c["after"] - c["before"]
                print(f"    {c['before']:5.1f} -> {c['after']:5.1f} "
                      f"({delta:+5.1f})  {(c['name'] or '')[:52]}")
        elif r["rescored"]:
            print("  no score changed — engine output is identical")

        if r["stale_features"]:
            print(f"\n  ⚠ {r['stale_features']} row(s) were extracted by an older"
                  f" analyser and cannot be rescored from cache.\n"
                  f"    Re-run Analyze on those folders to pick up the new"
                  f" features.")


if __name__ == "__main__":
    main()
