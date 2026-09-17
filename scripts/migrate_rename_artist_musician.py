"""
scripts/migrate_rename_artist_musician.py — 2026-09-16 vocabulary rename.

The third naming pass over these two entities (see the dated epoch table in
CONTEXT.md §2 and "Artist-Musician Rename and FLAC Tag Taxonomy — Design Spec
v1"). The act becomes Artist, the person becomes Musician. Values are
unchanged — only names.

Two passes, because `artist`, `artist_image` and `membership.artist_id` are
occupied when the swap begins:

  Pass A — person becomes Musician (frees the names)
    artist                         -> musician
    artist_image                   -> musician_image
    musician_image.artist_id       -> musician_id
    membership.artist_id           -> musician_id
    performance_personnel.artist_id-> musician_id

  Pass B — act becomes Artist (claims them)
    performer                      -> artist
    performer_image                -> artist_image
    performer_resource             -> artist_resource
    artist_image.performer_id      -> artist_id
    artist_resource.performer_id   -> artist_id
    membership.performer_id        -> artist_id
    performance.performer_id       -> artist_id
    user_artist_permission.performer_id -> artist_id

  Indexes — ALTER TABLE never renames an index, so the four affected ones are
  dropped and recreated under the names SQLAlchemy's `index=True` expects.
  All drops run before any create: after pass A the old
  `ix_artist_image_artist_id` sits on musician_image and would collide.

⚠ The 2026-07-09 rename ran with FK enforcement off, so SQLite never rewrote
FK clauses stored in OTHER tables' DDL and tools/repair_stale_fk_ddl.py had to
rebuild two tables. This script sets legacy_alter_table=OFF and
foreign_keys=ON before the transaction, and asserts afterwards: FK check empty,
no `performer` anywhere in sqlite_master, the expected columns and FK targets,
and unchanged row counts. Any failure rolls the whole thing back.

Photos: person photos would live under LIBRARY_ROOT/_artists/. On 2026-09-16
there were none (0 artist_image rows, no folder), so there is no folder move —
the script refuses to run if an `_artists/` folder has appeared since.

Dry-run is the default and is safe against the live database: it copies the
database into memory and runs the full migration and every assertion there.

Quit Trellis and take a backup first. Then, from the repo root:
    python3 scripts/migrate_rename_artist_musician.py            # dry run
    python3 scripts/migrate_rename_artist_musician.py --apply
    python3 scripts/migrate_rename_artist_musician.py --db PATH  # other database
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "db" / "trellis.db"
LIBRARY_ROOT = Path(os.environ.get("LIBRARY_ROOT", "/Volumes/music/Trellis/Library"))

PASS_A = [
    "ALTER TABLE artist RENAME TO musician",
    "ALTER TABLE artist_image RENAME TO musician_image",
    "ALTER TABLE musician_image RENAME COLUMN artist_id TO musician_id",
    "ALTER TABLE membership RENAME COLUMN artist_id TO musician_id",
    "ALTER TABLE performance_personnel RENAME COLUMN artist_id TO musician_id",
]

PASS_B = [
    "ALTER TABLE performer RENAME TO artist",
    "ALTER TABLE performer_image RENAME TO artist_image",
    "ALTER TABLE performer_resource RENAME TO artist_resource",
    "ALTER TABLE artist_image RENAME COLUMN performer_id TO artist_id",
    "ALTER TABLE artist_resource RENAME COLUMN performer_id TO artist_id",
    "ALTER TABLE membership RENAME COLUMN performer_id TO artist_id",
    "ALTER TABLE performance RENAME COLUMN performer_id TO artist_id",
    "ALTER TABLE user_artist_permission RENAME COLUMN performer_id TO artist_id",
]

INDEXES = [
    "DROP INDEX ix_performance_personnel_artist_id",
    "DROP INDEX ix_artist_image_artist_id",
    "DROP INDEX ix_performer_image_performer_id",
    "DROP INDEX ix_performer_mbid",
    "CREATE INDEX ix_musician_image_musician_id ON musician_image (musician_id)",
    "CREATE INDEX ix_performance_personnel_musician_id ON performance_personnel (musician_id)",
    "CREATE INDEX ix_artist_image_artist_id ON artist_image (artist_id)",
    "CREATE INDEX ix_artist_mbid ON artist (mbid)",
]

# Pre-migration table -> post-migration table, for the row-count assertion.
COUNTED = {
    "artist": "musician",
    "artist_image": "musician_image",
    "performer": "artist",
    "performer_image": "artist_image",
    "performer_resource": "artist_resource",
    "membership": "membership",
    "performance": "performance",
    "performance_personnel": "performance_personnel",
    "user_artist_permission": "user_artist_permission",
}

# table -> columns that must exist afterwards / must not
EXPECT_COLS = {
    "membership": ({"artist_id", "musician_id"}, {"performer_id"}),
    "performance": ({"artist_id"}, {"performer_id"}),
    "performance_personnel": ({"musician_id"}, {"artist_id"}),
    "musician_image": ({"musician_id"}, {"artist_id"}),
    "artist_image": ({"artist_id"}, {"performer_id"}),
    "artist_resource": ({"artist_id"}, {"performer_id"}),
    "user_artist_permission": ({"artist_id"}, {"performer_id"}),
}

# (table, column) -> table its FK must point at afterwards
EXPECT_FKS = {
    ("musician_image", "musician_id"): "musician",
    ("performance_personnel", "musician_id"): "musician",
    ("artist_image", "artist_id"): "artist",
    ("artist_resource", "artist_id"): "artist",
    ("performance", "artist_id"): "artist",
    ("user_artist_permission", "artist_id"): "artist",
}


def tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info('{table}')")}


def check_preconditions(con, apply):
    t = tables(con)
    if "musician" in t or "performer" not in t:
        sys.exit("Schema is not pre-rename (musician present or performer missing). Already migrated?")
    if con.execute("PRAGMA foreign_key_check").fetchall():
        sys.exit("foreign_key_check is NOT clean before starting. Fix that first so it is not blamed on this.")

    photos = LIBRARY_ROOT / "_artists"
    if not LIBRARY_ROOT.is_dir():
        msg = f"LIBRARY_ROOT not reachable ({LIBRARY_ROOT}) — cannot confirm there is no _artists/ photo folder."
        if apply:
            sys.exit(msg + " Mount the library and re-run.")
        print("WARNING: " + msg)
    elif photos.exists():
        sys.exit(f"{photos} exists. Person photos would be orphaned; this script has no folder move. Stop and decide.")


def run(con, label, statements):
    print(f"\n{label}")
    for sql in statements:
        print("  " + sql)
        con.execute(sql)


def assert_post(con, before):
    problems = []

    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        problems.append(f"foreign_key_check returned {len(fk)} rows, first: {fk[0]}")

    stale = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE instr(lower(name), 'performer') "
        "OR instr(lower(coalesce(sql, '')), 'performer')")]
    if stale:
        problems.append(f"'performer' still in sqlite_master: {stale}")

    for table, (must, mustnt) in EXPECT_COLS.items():
        cols = columns(con, table)
        if must - cols:
            problems.append(f"{table} missing {sorted(must - cols)}")
        if mustnt & cols:
            problems.append(f"{table} still has {sorted(mustnt & cols)}")

    for (table, col), target in EXPECT_FKS.items():
        refs = {r[2] for r in con.execute(f"PRAGMA foreign_key_list('{table}')") if r[3] == col}
        if refs != {target}:
            problems.append(f"{table}.{col} FK points at {refs or 'nothing'}, expected {target}")

    for old, new in COUNTED.items():
        n = con.execute(f"SELECT count(*) FROM {new}").fetchone()[0]
        if n != before[old]:
            problems.append(f"row count {old}->{new}: {before[old]} -> {n}")

    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="commit to the database (default: dry run in memory)")
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"No database at {args.db}")

    if args.apply:
        con = sqlite3.connect(str(args.db), isolation_level=None)
        print(f"APPLYING to {args.db.resolve()}")
    else:
        src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        con = sqlite3.connect(":memory:", isolation_level=None)
        src.backup(con)
        src.close()
        print(f"DRY RUN against an in-memory copy of {args.db.resolve()}")

    print(f"SQLite {sqlite3.sqlite_version}")
    # Both must be set OUTSIDE a transaction; foreign_keys is a no-op inside one.
    con.execute("PRAGMA legacy_alter_table=OFF")
    con.execute("PRAGMA foreign_keys=ON")
    if con.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        sys.exit("Could not enable foreign_keys.")

    check_preconditions(con, args.apply)
    before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in COUNTED}

    con.execute("BEGIN IMMEDIATE")
    try:
        run(con, "Pass A — person becomes Musician", PASS_A)
        run(con, "Pass B — act becomes Artist", PASS_B)
        run(con, "Indexes", INDEXES)
        problems = assert_post(con, before)
    except Exception:
        con.execute("ROLLBACK")
        raise

    print("\nRow counts")
    for old, new in COUNTED.items():
        print(f"  {old:24} -> {new:24} {before[old]}")

    if problems:
        con.execute("ROLLBACK")
        print("\nFAILED — rolled back:")
        for p in problems:
            print("  " + p)
        sys.exit(1)

    if args.apply:
        con.execute("COMMIT")
        print("\nintegrity:", con.execute("PRAGMA integrity_check").fetchone()[0])
        print("All assertions passed. Committed.")
    else:
        con.execute("ROLLBACK")
        print("\nAll assertions passed. Dry run — nothing written. Re-run with --apply.")
    con.close()


if __name__ == "__main__":
    main()
