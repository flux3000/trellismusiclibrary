"""scripts/diagnose_commons.py — show exactly where the photo chain breaks.

The Commons lookup is a four-link chain across two external APIs:

    performer.mb_links_json  →  Wikidata QID  →  P18/P373  →  Commons file  →  bytes

This prints each link separately so the two can never be confused again.

    python3 scripts/diagnose_commons.py                  # 5 well-known acts
    python3 scripts/diagnose_commons.py --limit 20
    python3 scripts/diagnose_commons.py --name "The Meters"

Read-only: fetches, reports, writes nothing.
"""

import sys
import json
import logging
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
from app.models.performer import Performer
from app.utils import commons


def diagnose(p):
    print(f"\n── {p.name}")
    print(f"   mb_status : {p.mb_status or 'never looked up'}")
    if not p.mbid:
        print("   ✗ no MusicBrainz match — nothing to follow")
        return "no-mbid"

    try:
        links = json.loads(p.mb_links_json) if p.mb_links_json else {}
    except (TypeError, ValueError):
        links = {}
    print(f"   links     : {', '.join(sorted(links)) or '(none)'}")

    qid = commons.qid_from_links(links)
    if not qid:
        print("   ✗ no Wikidata link in the MusicBrainz relations")
        return "no-wikidata"
    print(f"   QID       : {qid}  (https://www.wikidata.org/wiki/{qid})")

    names = commons.image_filenames_for_qid(qid)
    if not names:
        print("   ✗ Wikidata returned no P18 image and no Commons category")
        print("     (if this happens for EVERY act, suspect the API call, not the data)")
        return "no-images"
    print(f"   candidates: {len(names)} — {names[:3]}")

    for fname in names[:3]:
        info = commons.file_info(fname)
        if not info:
            print(f"   · {fname}: rejected (unreadable or non-free licence)")
            continue
        print(f"   ✓ {fname}")
        print(f"     licence : {info['licence']}")
        print(f"     credit  : {info['credit']}")
        print(f"     url     : {info['url']}")
        return "ok"
    return "all-rejected"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--name", help="diagnose one performer by name")
    ap.add_argument("--quiet", action="store_true",
                    help="hide the API warning log")
    args = ap.parse_args()

    # Surface commons.py's WARNING lines — an API parameter error is the single
    # most useful thing this script can show, and it is logged, not raised.
    if not args.quiet:
        logging.basicConfig(level=logging.INFO,
                            format="   [%(levelname)s] %(message)s")

    app = create_app()
    with app.app_context():
        q = db.session.query(Performer).filter(Performer.mbid.isnot(None))
        if args.name:
            rows = q.filter(Performer.name.ilike(f"%{args.name}%")).all()
        else:
            # Most-recorded first — the acts most likely to HAVE a photo, so a
            # zero result here is a real signal rather than a long-tail miss.
            rows = sorted(q.all(), key=lambda p: -len(p.performances))[:args.limit]

        if not rows:
            print("No matched performers found.")
            return 1

        tally = {}
        for p in rows:
            outcome = diagnose(p)
            tally[outcome] = tally.get(outcome, 0) + 1

        print("\n" + "─" * 60)
        for k, v in sorted(tally.items()):
            print(f"  {k}: {v}")
        if tally.get("no-images", 0) == len(rows) and len(rows) > 2:
            print("\n  ⚠ EVERY act failed at the Wikidata step. That is a broken")
            print("    API call, not missing data — check the warnings above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
