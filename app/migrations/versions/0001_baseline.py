"""baseline: bring any v0.2.0-or-later database up to the 2026-10-14 schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-14

Runs once, on a database that has no alembic_version (every install that existed before
Alembic). A database created from now on is stamped at head and never runs this.

It replaces, in one idempotent step, everything that used to bring a database up to date:
the ensure_* functions in app/utils/schema_upgrades.py and the additive scripts/migrate_*.py.
It works by inspection against the frozen schema in app/utils/baseline_schema.py:

  * a table that is missing is created;
  * a column that is missing is added (SQLite ADD COLUMN, so it has a default or is nullable);
  * an index that is missing is created.

Nothing that exists is altered or dropped. The data steps below run only when the column or
table they belong to had to be added, so a current database is never rewritten.

A database from before the Performer/Artist/Musician renames, or one missing a NOT NULL
column with no known default, predates v0.2.0: raise UnsupportedDatabaseError and change nothing.
Database only: nothing here reads or moves a file in the library.
"""
import re

import sqlalchemy as sa
from alembic import op

from app.utils.baseline_schema import BASELINE_DEFAULTS, SCHEMA
from app.utils.db_upgrade import MSG_TOO_OLD, UnsupportedDatabaseError, unsupported_reason

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

_DISC_SET_RE = re.compile(r"^(Disc|CD) (\d+)$")


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if unsupported_reason(tables):
        raise UnsupportedDatabaseError(MSG_TOO_OLD)

    created, added = set(), set()
    for table, (create_sql, columns, _indexes) in SCHEMA.items():
        if table not in tables:
            bind.exec_driver_sql(create_sql)
            created.add(table)
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for name, fragment, needs_default in columns:
            if name in have:
                continue
            if needs_default:
                default = BASELINE_DEFAULTS.get((table, name))
                if default is None:
                    raise UnsupportedDatabaseError(MSG_TOO_OLD)
                fragment += f" DEFAULT {default}"
            bind.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {fragment}")
            added.add((table, name))
    for table, (_create, _columns, indexes) in SCHEMA.items():
        for _name, sql in indexes:
            bind.exec_driver_sql(sql)

    # -- Data that goes with a column or table that was just added -----------------------------
    if ("artist", "confirmed_at") in added:
        # scripts/migrate_add_artist_confirmed.py: every existing artist counts as confirmed.
        bind.exec_driver_sql(
            "UPDATE artist SET confirmed_at = COALESCE(NULLIF(created_at, ''), "
            "strftime('%Y-%m-%d %H:%M:%f', 'now'))")
    if ("collection", "system_key") in added:
        _seed_full_library(bind)
    if ("track", "disc_number") in added:
        _file_handling_data(bind)
    _split_shared_studio_performances(bind)


def _seed_full_library(bind):
    """scripts/migrate_add_system_collections.py: the 'Full Library' system collection."""
    row = bind.exec_driver_sql("SELECT 1 FROM collection WHERE system_key = 'full_library'").first()
    if row is None:
        bind.exec_driver_sql(
            "INSERT INTO collection (name, description, system_key, created_at, updated_at) "
            "VALUES ('Full Library', 'Everything currently on the shelf. Membership is resolved live, "
            "so shows added later appear automatically and shows moved out to Workshop or Backlog "
            "drop out.', 'full_library', datetime('now'), datetime('now'))")


def _file_handling_data(bind):
    """scripts/migrate_file_handling.py, the database-only steps (the fingerprint rel_path
    backfill reads the library, so it is not done here)."""
    # Retired preference.
    bind.exec_driver_sql("DELETE FROM user_preference WHERE key = 'ingest_file_behavior'")
    # An existing library already looks organized; seed that choice if it was never made.
    n = bind.exec_driver_sql("SELECT count(*) FROM recording").scalar()
    if n and bind.exec_driver_sql(
            "SELECT 1 FROM node_setting WHERE key = 'file_handling_mode'").first() is None:
        for key, value in (("file_handling_mode", "organize"), ("rename_folders", "true"),
                           ("rename_files", "true"), ("naming_scheme", "number_title"),
                           ("write_tags_on_ingest", "true"), ("write_tags_default", "true")):
            bind.exec_driver_sql(
                "INSERT INTO node_setting (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
                (key, value))
    # 'Disc 2' / 'CD 2' in track.set_number becomes disc_number + disc_track_number.
    by_disc = {}
    for row_id, rec_id, track_number, set_number in bind.exec_driver_sql(
            "SELECT id, recording_id, track_number, set_number FROM track "
            "WHERE set_number IS NOT NULL").fetchall():
        m = _DISC_SET_RE.match(set_number)
        if m:
            by_disc.setdefault((rec_id, int(m.group(2))), []).append((track_number, row_id))
    for (_rec_id, disc), members in by_disc.items():
        for disc_track, (_tn, row_id) in enumerate(sorted(members, key=lambda p: (p[0] is None, p[0])), start=1):
            bind.exec_driver_sql(
                "UPDATE track SET disc_number = ?, disc_track_number = ?, set_number = NULL WHERE id = ?",
                (disc, disc_track, row_id))


def _split_shared_studio_performances(bind):
    """scripts/migrate_studio_records.py (B1): a Performance shared by more than one studio
    recording is split so each studio recording has its own. Idempotent; a no-op when there
    is nothing shared."""
    groups = {}
    for perf_id, rec_id in bind.exec_driver_sql(
            "SELECT performance_id, id FROM recording WHERE kind = 'studio' "
            "ORDER BY performance_id, id").fetchall():
        groups.setdefault(perf_id, []).append(rec_id)
    cols = ("artist_id", "start_year", "start_month", "start_day", "end_year", "end_month",
            "end_day", "city", "state", "country", "personnel_mode")
    for perf_id, rec_ids in groups.items():
        if len(rec_ids) < 2:
            continue
        values = bind.exec_driver_sql(
            f"SELECT {', '.join(cols)} FROM performance WHERE id = ?", (perf_id,)).fetchone()
        for rec_id in rec_ids[1:]:
            new_id = bind.exec_driver_sql(
                "INSERT INTO performance (artist_id, venue_id, start_year, start_month, start_day, "
                "end_year, end_month, end_day, city, state, country, personnel_mode) "
                "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", tuple(values)).lastrowid
            bind.exec_driver_sql("UPDATE recording SET performance_id = ? WHERE id = ?", (new_id, rec_id))
