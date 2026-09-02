"""
prune_orphan_staging.py — Delete staging rows whose folder is gone from disk.

`quality_analysis` rows are keyed by folder path and deliberately OUTLIVE the
folder: a Move ingest relocates the source out from under its own row, and
`promote_to_recording` needs the row to still be there when the commit lands.
The cost is that a row for a folder that vanished any OTHER way — ingested
from a different path, moved by hand, a download deleted — never gets cleared.
`list_staging` then keeps offering it on every scan of the same parent
directory, wearing "Folder moved" and offering nothing to act on.

Ryan, 2026-09-02: a Pat Metheny show from several jobs ago that would not go
away.  `api/quality.py` now scopes each poll's results to the folders that run
actually resolved, so nothing off-disk can reappear in the queue; this script
is the separate, one-off cleanup of the rows already in the table.

⚠ It only ever deletes rows with `recording_id IS NULL` — a promoted row is
the permanent record of where an ingested show came from and is not staging
work at all.

⚠ An unmounted volume looks exactly like a deleted folder to `os.path.isdir`.
So a row is only considered when its SOURCE DIR is present: if
/Volumes/music is not mounted, every row under it is skipped rather than
mistaken for garbage.  That guard is the whole reason this is a script you run
deliberately and not a sweep on boot.

    python3 scripts/prune_orphan_staging.py            # report only
    python3 scripts/prune_orphan_staging.py --delete   # actually delete
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app                       # noqa: E402
from app.extensions import db                    # noqa: E402
from app.models.quality import QualityAnalysis   # noqa: E402
from config import Config                        # noqa: E402


def main():
    delete = "--delete" in sys.argv
    app = create_app()
    with app.app_context():
        print(f"database: {Config.DB_PATH}")

        rows = (db.session.query(QualityAnalysis)
                .filter(QualityAnalysis.recording_id.is_(None))
                .all())

        orphans, skipped_dirs = [], set()
        for r in rows:
            src = r.source_dir
            # Volume-not-mounted guard — see the module docstring.
            if not src or not os.path.isdir(src):
                skipped_dirs.add(src)
                continue
            if not os.path.isdir(r.folder_path):
                orphans.append(r)

        print(f"unpromoted staging rows: {len(rows)}")
        if skipped_dirs:
            print(f"skipped (source dir not present, volume may be unmounted): "
                  f"{len(skipped_dirs)} directories")
            for d in sorted(skipped_dirs):
                print(f"    - {d}")
        print(f"orphans (folder gone, source dir present): {len(orphans)}")
        for r in orphans:
            print(f"    [{r.triage_status}] {r.folder_path}")

        if not orphans:
            print("nothing to do.")
            return
        if not delete:
            print("\ndry run — re-run with --delete to remove these rows.")
            return

        for r in orphans:
            db.session.delete(r)
        db.session.commit()
        print(f"\ndeleted {len(orphans)} row(s).")


if __name__ == "__main__":
    main()
