"""
scripts/migrate_studio_records.py -- Studio Records spec v1, chunk 1 (sharing
policy).

Same shape as scripts/migrate_bulk_ingest.py: dry run by default, one
transaction, WAL refusal + backup on --apply, assertions before commit.

Column list (COLUMN_SPECS below) is shared with the other two build agents
working the same spec at the same time (MusicBrainz release lookup, recording
artwork): each of their new `recording` columns gets its own
(table, column, ddl) tuple appended to the list, and `add_columns()` below
adds each one with its own idempotent existence check -- so this file stays
one script with one shared list rather than three competing migrations.

Also creates `recording_image` (Design Spec v1 section 5 / chunk 4 --
recording-level artwork) if it is absent, the same way
scripts/migrate_add_venue_images.py creates `venue_image`: normal installs
never need this (SQLAlchemy's create_all() already makes the table on first
run, since it is registered in app/models/__init__.py), but an existing
install whose db predates this model and never runs create_all again would
otherwise be missing it after an --apply of just this script.

No-op on a second run: every ALTER is guarded by an existence check, and the
CREATE TABLE is IF NOT EXISTS.

Quit Trellis and take a backup first. Then, from the repo root:
    python3 scripts/migrate_studio_records.py            # dry run
    python3 scripts/migrate_studio_records.py --apply
    python3 scripts/migrate_studio_records.py --db PATH  # other database
"""

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "db" / "trellis.db"

# (table, column, ddl) -- one idempotent ALTER TABLE per entry. Other build
# agents append their own recording columns here (e.g. the mb_release_*
# columns, recording-artwork fields) rather than writing a second script.
COLUMN_SPECS = [
    # MusicBrainz release lookup (Design Spec v1 section 2 / section 5).
    ("recording", "mb_release_id",         "VARCHAR(36)"),
    ("recording", "mb_release_group_id",   "VARCHAR(36)"),
    ("recording", "mb_release_status",     "VARCHAR(16)"),
    ("recording", "mb_release_type",       "VARCHAR(32)"),
    ("recording", "mb_label",              "VARCHAR(120)"),
    ("recording", "mb_catalog_number",     "VARCHAR(64)"),
    ("recording", "mb_release_country",    "VARCHAR(2)"),
    ("recording", "mb_release_checked_at", "DATETIME"),
    # Artwork backfill follow-up pass (S8, independent review v1). NULL
    # until the "images" follow-up kind has run for this recording (whether
    # or not it found anything) -- same never-looked-up-yet contract as
    # mb_release_checked_at above.
    ("recording", "images_checked_at",     "DATETIME"),
]

# Tables whose row counts must be unchanged after this migration -- an ALTER
# TABLE ADD COLUMN never touches rows, so any drift here means something else
# went wrong.
ROW_COUNT_TABLES = ("recording",)


def columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info('{table}')")}


RECORDING_IMAGE_DDL = """
    CREATE TABLE IF NOT EXISTS recording_image (
        id            INTEGER PRIMARY KEY,
        recording_id  INTEGER NOT NULL
                      REFERENCES recording(id) ON DELETE CASCADE,
        filename      VARCHAR(255) NOT NULL,
        ext           VARCHAR(8)   NOT NULL,
        is_primary    BOOLEAN NOT NULL DEFAULT 0,
        sort_order    INTEGER NOT NULL DEFAULT 0,
        origin        VARCHAR(24) NOT NULL DEFAULT 'upload',
        source_ref    VARCHAR(512),
        caption       VARCHAR(255),
        credit        VARCHAR(255),
        created_at    DATETIME
    )
"""


def create_recording_image_table(con):
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    print("\nTable")
    if "recording_image" in tables:
        print("  recording_image already present")
        return False
    print("  CREATE TABLE recording_image")
    con.execute(RECORDING_IMAGE_DDL)
    con.execute("CREATE INDEX IF NOT EXISTS ix_recording_image_recording_id "
                "ON recording_image(recording_id)")
    return True


def find_shared_studio_performances(con):
    """
    Every Performance carrying more than one studio recording, oldest
    recording first within each group (that one keeps the Performance;
    every other studio recording on it gets split onto its own copy -- B1).

    Returns {perf_id: [recording_id, ...]} (2+ entries each), recording ids
    sorted ascending so index 0 is "kept, untouched". Empty on a database
    that predates the `kind` column entirely -- there is nothing to split
    apart on a build that has never had studio recordings at all.
    """
    rec_cols = columns(con, "recording")
    if "kind" not in rec_cols or "performance_id" not in rec_cols:
        return {}
    rows = con.execute("""
        SELECT r.performance_id, r.id
        FROM recording r
        WHERE r.kind = 'studio'
        ORDER BY r.performance_id, r.id
    """).fetchall()
    groups = {}
    for perf_id, rec_id in rows:
        groups.setdefault(perf_id, []).append(rec_id)
    return {perf_id: recs for perf_id, recs in groups.items() if len(recs) > 1}


def split_shared_studio_performances(con, apply):
    """
    B1 backfill: a Performance already shared by more than one studio
    recording (from before app/api/ingest.py stopped ever creating one)
    gets split apart -- the first (lowest-id) recording keeps the original
    Performance row, and every other studio recording on it is repointed to
    a brand new Performance copying that artist, date (start/end
    year/month/day), city/state/country and personnel_mode, with venue_id
    left null (a studio release is not tied to the venue a live show at
    that Performance might have been).

    Idempotent: once every Performance carries at most one studio
    recording, find_shared_studio_performances() returns nothing and a
    second run is a no-op. Dry run only prints what would split -- no
    write, matching every other step in this script.
    """
    groups = find_shared_studio_performances(con)
    print("\nShared studio Performances")
    if not groups:
        print("  none found")
        return
    cols = ("artist_id", "start_year", "start_month", "start_day",
            "end_year", "end_month", "end_day",
            "city", "state", "country", "personnel_mode")
    for perf_id, rec_ids in groups.items():
        keep, split_off = rec_ids[0], rec_ids[1:]
        print(f"  performance {perf_id}: keep recording {keep}, "
              f"split off {split_off}")
        if not apply:
            continue
        row = con.execute(
            f"SELECT {', '.join(cols)} FROM performance WHERE id = ?",
            (perf_id,)).fetchone()
        values = dict(zip(cols, row))
        for rec_id in split_off:
            new_id = con.execute(
                "INSERT INTO performance "
                "(artist_id, venue_id, start_year, start_month, start_day, "
                " end_year, end_month, end_day, city, state, country, "
                " personnel_mode) "
                "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (values["artist_id"], values["start_year"], values["start_month"],
                 values["start_day"], values["end_year"], values["end_month"],
                 values["end_day"], values["city"], values["state"],
                 values["country"], values["personnel_mode"])
            ).lastrowid
            con.execute("UPDATE recording SET performance_id = ? WHERE id = ?",
                        (new_id, rec_id))
            print(f"    recording {rec_id} -> new performance {new_id}")


def add_columns(con, specs):
    print("\nColumns")
    added = False
    for table, column, ddl in specs:
        if column in columns(con, table):
            print(f"  {table}.{column} already present")
            continue
        sql = f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
        print("  " + sql)
        con.execute(sql)
        added = True
    if not added:
        print("  nothing to add")
    return added


def assert_post(con, before, specs):
    problems = []
    for table, column, _ddl in specs:
        if column not in columns(con, table):
            problems.append(f"{table} missing {column!r}")

    for table in ROW_COUNT_TABLES:
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if n != before[table]:
            problems.append(f"row count changed for {table}: {before[table]} -> {n}")

    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true",
                    help="commit to the database (default: dry run, nothing written)")
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"No database at {args.db}")

    if args.apply:
        # Same non-empty-WAL refusal and sqlite3 .backup() snapshot as
        # migrate_bulk_ingest.py -- see that script's comments for why.
        wal_path = args.db.with_name(args.db.name + "-wal")
        if wal_path.exists() and wal_path.stat().st_size > 0:
            sys.exit(f"{wal_path} is non-empty -- quit Trellis first (or checkpoint the WAL) "
                      "before applying this migration.")
        backup_path = args.db.with_name(args.db.name + ".pre-studio-records-migration.bak")
        backup_src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        backup_dst = sqlite3.connect(str(backup_path))
        with backup_dst:
            backup_src.backup(backup_dst)
        backup_src.close()
        backup_dst.close()
        print(f"Backed up {args.db} -> {backup_path}")
        con = sqlite3.connect(str(args.db), isolation_level=None)
        print(f"APPLYING to {args.db.resolve()}")
    else:
        src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        con = sqlite3.connect(":memory:", isolation_level=None)
        src.backup(con)
        src.close()
        print(f"DRY RUN against an in-memory copy of {args.db.resolve()}")

    print(f"SQLite {sqlite3.sqlite_version}")
    con.execute("PRAGMA foreign_keys=ON")

    before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ROW_COUNT_TABLES}

    con.execute("BEGIN IMMEDIATE")
    try:
        add_columns(con, COLUMN_SPECS)
        create_recording_image_table(con)
        split_shared_studio_performances(con, apply=args.apply)
        problems = assert_post(con, before, COLUMN_SPECS)
        if "recording_image" not in {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}:
            problems.append("recording_image table missing after create")
        if args.apply and find_shared_studio_performances(con):
            problems.append("a Performance still carries more than one "
                            "studio recording after the split")
    except Exception:
        con.execute("ROLLBACK")
        raise

    if problems:
        con.execute("ROLLBACK")
        print("\nFAILED -- rolled back:")
        for p in problems:
            print("  " + p)
        sys.exit(1)

    if args.apply:
        con.execute("COMMIT")
        print("\nintegrity:", con.execute("PRAGMA integrity_check").fetchone()[0])
        print("All assertions passed. Committed.")
    else:
        con.execute("ROLLBACK")
        print("\nAll assertions passed (dry run -- nothing written).")


if __name__ == "__main__":
    main()
