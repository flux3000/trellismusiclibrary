"""
scripts/migrate_add_performance_personnel.py — Per-Show Personnel, Phase 1.

Adds (all additive, idempotent, safe to re-run):
  membership.start_year/start_month/start_day/end_year/end_month/end_day
      (nullable) — a Membership row becomes a STINT. NULL throughout on every
      existing row = "always a member," identical to today's behavior.
  performer.default_personnel_mode  VARCHAR(16) NOT NULL DEFAULT 'inherit'
  performance.personnel_mode        VARCHAR(16) NOT NULL DEFAULT 'inherit'
  performance_personnel table (new) — show-level lineup rows.

Nothing is backfilled and no existing row changes meaning. See
"Context Library/2026-07-17 - Per-Show Personnel — Design Plan (DRAFT).md" §4 ("Why the
migration is safe") and §7 (decisions).

Run once from the repo root:
    python3 scripts/migrate_add_performance_personnel.py
"""

import os
import sqlite3

DB = os.environ.get(
    "FLUX_DB",
    os.path.join(os.path.dirname(__file__), "..", "db", "fluxaudio.db"),
)


def _existing_columns(cur, table):
    return {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}


def _add_column(cur, table, col_def):
    col_name = col_def.split()[0]
    if col_name in _existing_columns(cur, table):
        print(f"{table}.{col_name} already exists")
        return
    cur.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
    print(f"added {table}.{col_name}")


def _has_table(cur, name):
    return cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()

    # --- membership: stint date bounds ---
    for col_def in [
        "start_year INTEGER",
        "start_month INTEGER",
        "start_day INTEGER",
        "end_year INTEGER",
        "end_month INTEGER",
        "end_day INTEGER",
    ]:
        _add_column(cur, "membership", col_def)

    # --- performer: act-level default resolution mode ---
    _add_column(cur, "performer",
                "default_personnel_mode VARCHAR(16) NOT NULL DEFAULT 'inherit'")

    # --- performance: per-show resolution mode ---
    _add_column(cur, "performance",
                "personnel_mode VARCHAR(16) NOT NULL DEFAULT 'inherit'")

    # --- performance_personnel: new table ---
    if _has_table(cur, "performance_personnel"):
        print("performance_personnel already exists")
    else:
        cur.execute("""
            CREATE TABLE performance_personnel (
                id             INTEGER PRIMARY KEY,
                performance_id INTEGER NOT NULL REFERENCES performance(id),
                artist_id      INTEGER NOT NULL REFERENCES artist(id),
                instrument     VARCHAR(128),
                "order"        INTEGER NOT NULL DEFAULT 0,
                is_guest       BOOLEAN NOT NULL DEFAULT 0,
                note           VARCHAR(255),
                created_at     DATETIME
            )""")
        print("created performance_personnel")

    for stmt in [
        "CREATE INDEX IF NOT EXISTS ix_performance_personnel_performance_id "
        "ON performance_personnel(performance_id)",
        "CREATE INDEX IF NOT EXISTS ix_performance_personnel_artist_id "
        "ON performance_personnel(artist_id)",
    ]:
        cur.execute(stmt)
    print("indexes ensured")

    con.commit()
    con.close()
    print("done")


if __name__ == "__main__":
    main()
