"""Renames the existing schema to match the renamed ORM models:
  table  artist            -> canonical_artist
  table  performer         -> artist
  table  performer_artist  -> artist_canonical
    col    performer_artist.artist_id     -> canonical_artist_id
    col    performer_artist.performer_id  -> artist_id
  col    performance.performer_id             -> artist_id
  col    user_artist_permission.artist_id     -> canonical_artist_id

Values are unchanged — only names. SQLite (3.25+) auto-updates FK references in
other tables on RENAME. Order matters: free the `artist` name (rename canonical
first), and rename the junction's artist_id before performer_id to avoid a
column-name collision.

Run once:
    python3 scripts/migrate_rename_artist_performer.py
    python3 scripts/migrate_rename_artist_performer.py --dry-run
"""

import sys
import sqlite3
from pathlib import Path

DB = Path(__file__).parent.parent / "db" / "fluxaudio.db"

STEPS = [
    "ALTER TABLE artist RENAME TO canonical_artist",
    "ALTER TABLE performer RENAME TO artist",
    "ALTER TABLE performer_artist RENAME COLUMN artist_id TO canonical_artist_id",
    "ALTER TABLE performer_artist RENAME COLUMN performer_id TO artist_id",
    "ALTER TABLE performer_artist RENAME TO artist_canonical",
    "ALTER TABLE performance RENAME COLUMN performer_id TO artist_id",
    "ALTER TABLE user_artist_permission RENAME COLUMN artist_id TO canonical_artist_id",
]


def main():
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(str(DB))
    cur = con.cursor()

    # Guard: only run on the pre-rename schema.
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "performer" not in tables:
        print("Nothing to do — `performer` table not present (already migrated?).")
        return

    for sql in STEPS:
        print(("[dry-run] " if dry else "") + sql)
        if not dry:
            cur.execute(sql)

    if dry:
        con.rollback()
        print("\nDry run — no changes committed.")
    else:
        con.commit()
        print("\nintegrity:", cur.execute("PRAGMA integrity_check").fetchone()[0])
        final = sorted(r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
        print("tables:", final)
    con.close()


if __name__ == "__main__":
    main()
