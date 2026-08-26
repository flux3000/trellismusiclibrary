"""
backfill_track_flags.py — Apply auto-detected flags to existing tracks.

Uses app.utils.ingest.detect_track_flags() — the same conservative,
title-text-only detection used to pre-suggest flags in the ingest wizard
(see app/api/ingest.py / app/static/js/app.js::detectTrackFlags for the
client-side port used at ingest time).

Only touches tracks that currently have NO flags set. Tracks with existing
flags are left alone — those reflect a human's prior judgment call (and in
at least one case in this library, a flag that doesn't match the title text,
e.g. track titled "Audience" flagged "tuning" — not this script's place to
second-guess that).

Run once:
    cd ~/Workshop/dev/trellis
    python3 scripts/backfill_track_flags.py --dry-run   # preview only
    python3 scripts/backfill_track_flags.py              # apply
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
from app.models.track import Track
from app.utils.ingest import detect_track_flags


def main():
    dry_run = "--dry-run" in sys.argv
    app = create_app()
    with app.app_context():
        tracks = db.session.query(Track).all()
        changed = 0

        for t in tracks:
            existing = json.loads(t.flags) if t.flags else []
            if existing:
                continue  # respect existing curation, even if it looks off

            detected = detect_track_flags(t.title)
            if not detected:
                continue

            print(f"  track {t.id:5} {t.title!r:60} -> {detected}")
            changed += 1
            if not dry_run:
                t.flags = json.dumps(detected)

        print(f"\n{'[dry-run] Would update' if dry_run else 'Updated'} {changed} track(s) "
              f"out of {len(tracks)} total.")

        if not dry_run:
            db.session.commit()
            print("Committed.")
        else:
            db.session.rollback()


if __name__ == "__main__":
    main()
