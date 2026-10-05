"""
app/utils/schema_upgrades.py -- additive schema upgrades that run at every start.

create_all() cannot add a column to a table that exists, and first_run_setup() runs only
when the database file is absent, so a column added to an existing model needs a check
here. Each upgrade is idempotent, runs in one transaction, and is a no-op on a database
that already has it or has no table yet (a fresh one: create_all() builds it complete).

ensure_artist_confirmed_at (Resolver v2 chunk 7b): artist.confirmed_at, backfilled on every
existing row with created_at, else the migration time. scripts/migrate_add_artist_confirmed.py
does the same by hand; the app does not depend on it.

Failure behaviour: a database that does not exist yet (no file, or no folder) is left alone and
never created here; first_run_setup() and create_all() build it. A real migration error (read-only
file, a lock held past the 60 s timeout, a failed ALTER) is logged with the database path and
raised as RuntimeError: an app that errors on every Artist query is worse than a clear stop.
"""
import logging
import os
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _sqlite_path(engine):
    url = engine.url
    if not url.drivername.startswith("sqlite"):
        return None
    db = url.database
    return None if (not db or db == ":memory:") else db


def ensure_artist_confirmed_at(engine):
    """Add artist.confirmed_at and backfill it when the column is missing.
    Returns the number of rows backfilled, or None when nothing was needed.
    Raises only on a real database error (after logging it)."""
    path = _sqlite_path(engine)
    if path is None or not os.path.isfile(path):
        return None
    # mode=rw can never create a file: a path that vanished since the check is an error, not a new DB.
    con = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=60, isolation_level=None)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(artist)")}
        if not cols or "confirmed_at" in cols:
            return None
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
        con.execute("BEGIN IMMEDIATE")
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(artist)")}
            if "confirmed_at" in cols:          # another process got there first
                con.execute("ROLLBACK")
                return None
            con.execute("ALTER TABLE artist ADD COLUMN confirmed_at DATETIME")
            n = con.execute("UPDATE artist SET confirmed_at = COALESCE(NULLIF(created_at, ''), ?)",
                            (now,)).rowcount
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        log.warning("schema: added artist.confirmed_at and backfilled %d row(s) in %s", n, path)
        return n
    except sqlite3.Error as e:
        msg = f"Could not add the artist.confirmed_at column to the database at {path}: {e}"
        log.error("schema: %s", msg)
        raise RuntimeError(msg) from e
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
