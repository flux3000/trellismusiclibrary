"""
migrate_add_artist_confirmed.py -- add artist.confirmed_at and backfill it.

Resolver v2, chunk 7b (2026-10-05). confirmed_at records when a person first confirmed an
Artist row (a hand save of a recording under it, or a direct edit). The resolver counts an
exact match to a confirmed Artist as an independent source for the artist. See
app/models/artist.py and mark_artist_confirmed() in app/utils/artists.py.

Run it against the database the app uses (from the repo, .venv active, app stopped):

    python3 scripts/migrate_add_artist_confirmed.py [--dry-run]

The database is Config.DB_PATH (TRELLIS_DB_PATH overrides it). Additive and idempotent:

1. ALTER TABLE artist ADD COLUMN confirmed_at DATETIME (if absent).
2. Backfill: every row whose confirmed_at is NULL gets its created_at, or the migration time
   when created_at is empty. A person reviewed all of an existing library, so every existing
   row counts as confirmed. Rows an unattended import creates later start NULL.
3. Assert the column exists and no existing row is left NULL; exits non-zero otherwise.

The app does the same check at every start (app/utils/schema_upgrades.py), so running this
is optional; it is kept for doing it by hand with the app stopped. A fresh install gets the
column from create_all().
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config


def migrate(db_path, dry=False, out=print):
    """Returns (added column?, rows backfilled, rows still NULL)."""
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(artist)")}
        if not cols:
            raise SystemExit("no artist table in this database")
        added = "confirmed_at" not in cols
        if added:
            out("  + ALTER TABLE artist ADD COLUMN confirmed_at DATETIME")
            if not dry:
                cur.execute("ALTER TABLE artist ADD COLUMN confirmed_at DATETIME")
        else:
            out("  . artist.confirmed_at already exists")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
        n = 0
        if not dry:
            cur.execute("UPDATE artist SET confirmed_at = COALESCE(NULLIF(created_at, ''), ?) "
                        "WHERE confirmed_at IS NULL", (now,))
            n = cur.rowcount
            con.commit()
        left = 0 if dry and added else cur.execute(
            "SELECT COUNT(*) FROM artist WHERE confirmed_at IS NULL").fetchone()[0]
        return added, n, left
    finally:
        con.close()


def main():
    dry = "--dry-run" in sys.argv
    print(f"database: {Config.DB_PATH}")
    print(f"mode: {'dry run' if dry else 'write'}")
    added, n, left = migrate(Config.DB_PATH, dry)
    with sqlite3.connect(str(Config.DB_PATH)) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(artist)")}
        total = con.execute("SELECT COUNT(*) FROM artist").fetchone()[0]
    print(f"  artist columns include confirmed_at: {'confirmed_at' in cols}")
    print(f"  backfilled {n} of {total} artist rows; {left} still NULL")
    if dry:
        print("dry run: nothing written")
        return 0
    return 0 if ("confirmed_at" in cols and left == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
