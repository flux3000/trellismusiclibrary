"""
scripts/migrate_mb_release_db_status.py -- adds recording.mb_release_db_status.

Holds MusicBrainz's own release status (Official, Bootleg, ...), stored when a
release is linked, so the peer-share policy (app/utils/share_policy.py) can
decide without calling MusicBrainz. Existing linked rows stay NULL, which the
policy treats as not shareable until the release is linked again.

NEEDS OWNER APPROVAL BEFORE --apply. The Recording model reads this column, so
the app will not run against a database that lacks it: migrate first, then
start the app. Dry run by default; --apply backs up and refuses a WAL database
that is open elsewhere (quit Trellis first).

    python3 scripts/migrate_mb_release_db_status.py --db PATH           # dry run
    python3 scripts/migrate_mb_release_db_status.py --db PATH --apply
"""

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "db" / "trellis.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        sys.exit(f"database not found: {path}")

    con = sqlite3.connect(str(path))
    cols = {r[1] for r in con.execute("PRAGMA table_info('recording')")}
    if "mb_release_db_status" in cols:
        print("recording.mb_release_db_status already present")
        return
    print("ADD COLUMN recording.mb_release_db_status VARCHAR(24)")
    if not args.apply:
        print("dry run: nothing changed (pass --apply)")
        return

    before = con.execute("SELECT COUNT(*) FROM recording").fetchone()[0]
    backup = path.with_name(path.name + ".bak-" + datetime.now().strftime("%Y%m%d%H%M%S"))
    src = sqlite3.connect(str(path))
    dst = sqlite3.connect(str(backup))
    src.backup(dst)
    dst.close()
    src.close()
    print(f"backup: {backup}")

    with con:
        con.execute("ALTER TABLE recording ADD COLUMN mb_release_db_status VARCHAR(24)")
    after = con.execute("SELECT COUNT(*) FROM recording").fetchone()[0]
    assert before == after, "recording row count changed"
    print("applied")


if __name__ == "__main__":
    main()
