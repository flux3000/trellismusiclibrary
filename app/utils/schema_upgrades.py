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


# ── Lomax (2026-10-05) ───────────────────────────────────────────────────────

# (table, json column, skill, subject_type) of the three columns Lomax runs replace.
_LOMAX_LEGACY = (("recording", "ai_research_json", "recording", "recording"),
                 ("artist", "dossier_json", "artist", "artist"),
                 ("artist", "lineup_json", "artist", "artist"))


def _stamp(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S.%f")


def ensure_lomax(engine):
    """Create lomax_run and lomax_proposal when missing, copy the three legacy JSON columns
    (recording.ai_research_json, artist.dossier_json, artist.lineup_json) into done runs, and mark
    any run left queued or running by a previous process as interrupted.

    Idempotent: a copied row is recognised by lomax_run.migrated_from and never copied twice, and
    the legacy columns are left in place (nothing reads them). A no-op on a database that does not
    exist yet or has no artist table (create_all() builds those complete). Returns
    {"copied": n, "interrupted": n}, or None when it did nothing."""
    import json
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    path = _sqlite_path(engine)
    if path is None or not os.path.isfile(path):
        return None
    try:
        with engine.connect() as con:
            if con.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artist'")).first() is None:
                return None
        from app.models.lomax import LomaxProposal, LomaxRun
        LomaxRun.__table__.create(engine, checkfirst=True)
        LomaxProposal.__table__.create(engine, checkfirst=True)
        with engine.begin() as con:     # a table made before `challenge` or `agrees` existed
            have = {r[1] for r in con.execute(text("PRAGMA table_info(lomax_proposal)"))}
            for col in ("challenge", "agrees"):
                if col not in have:
                    con.execute(text(f"ALTER TABLE lomax_proposal ADD COLUMN {col} BOOLEAN NOT NULL DEFAULT 0"))
            # level read -> study and skill resolution -> recording (the two skills merged)
            if {"level", "skill"} <= {r[1] for r in con.execute(text("PRAGMA table_info(lomax_run)"))}:
                con.execute(text("UPDATE lomax_run SET level='study' WHERE level='read'"))
                con.execute(text("UPDATE lomax_run SET skill='recording' WHERE skill='resolution'"))

        copied = 0
        with engine.begin() as con:
            for table, col, skill, subject_type in _LOMAX_LEGACY:
                cols = {r[1] for r in con.execute(text(f"PRAGMA table_info({table})"))}
                if col not in cols:
                    continue
                done = {r[0] for r in con.execute(
                    text("SELECT subject_id FROM lomax_run WHERE migrated_from = :c"), {"c": col})}
                stamp_col = next((c for c in ("updated_at", "created_at") if c in cols), "NULL")
                for rid, blob, stamp in con.execute(text(
                        f"SELECT id, {col}, {stamp_col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''")).fetchall():
                    if rid in done:
                        continue
                    try:
                        data = json.loads(blob)
                    except (ValueError, TypeError):
                        continue            # a corrupt blob is not worth a run
                    if not isinstance(data, dict):
                        continue
                    when = stamp if isinstance(stamp, str) and stamp else _stamp()
                    con.execute(text(
                        "INSERT INTO lomax_run (skill, subject_type, subject_id, level, status, result_json, "
                        "usage_json, model, created_at, finished_at, migrated_from) VALUES "
                        "(:skill, :st, :sid, 'research', 'done', :res, :usage, :model, :ts, :ts, :col)"),
                        {"skill": skill, "st": subject_type, "sid": rid, "res": blob,
                         "usage": json.dumps(data["usage"]) if data.get("usage") else None,
                         "model": data.get("model"), "ts": when, "col": col})
                    copied += 1
            interrupted = con.execute(text(
                "UPDATE lomax_run SET status='error', error='Interrupted', finished_at=:ts "
                "WHERE status IN ('queued','running')"), {"ts": _stamp()}).rowcount
        if copied or interrupted:
            log.warning("schema: lomax: copied %d legacy result(s), marked %d run(s) interrupted in %s",
                        copied, interrupted, path)
        return {"copied": copied, "interrupted": interrupted}
    except SQLAlchemyError as e:
        msg = f"Could not set up Lomax in the database at {path}: {e}"
        log.error("schema: %s", msg)
        raise RuntimeError(msg) from e


def ensure_venue_history(engine):
    """Add venue.history (Lomax Venue History text) when the column is missing. Returns True when
    added, None when nothing was needed (no file, no venue table, or already there)."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    path = _sqlite_path(engine)
    if path is None or not os.path.isfile(path):
        return None
    try:
        with engine.begin() as con:
            cols = {r[1] for r in con.execute(text("PRAGMA table_info(venue)"))}
            if not cols or "history" in cols:
                return None
            con.execute(text("ALTER TABLE venue ADD COLUMN history TEXT"))
        log.warning("schema: added venue.history in %s", path)
        return True
    except SQLAlchemyError as e:
        msg = f"Could not add the venue.history column to the database at {path}: {e}"
        log.error("schema: %s", msg)
        raise RuntimeError(msg) from e
