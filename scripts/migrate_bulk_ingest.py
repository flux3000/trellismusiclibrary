"""
scripts/migrate_bulk_ingest.py -- Bulk Ingest spec chunk 2.

Same shape as scripts/migrate_file_handling.py: dry run by default, one
transaction, WAL refusal + backup on --apply, assertions before commit.

1. ALTER TABLE recording ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'live'
   (if absent), and CREATE TABLE for bulk_ingest_run / bulk_ingest_item (if
   absent) -- see app/models/bulk_ingest.py for the shape these mirror.
2. Move existing entity image folders out of the library and into DATA_DIR:
     LIBRARY_ROOT/_venues/<name>/_images    -> DATA_DIR/images/venues/<name>/
     LIBRARY_ROOT/_events/<name>/_images    -> DATA_DIR/images/events/<name>/
     LIBRARY_ROOT/_musicians/<name>/_images -> DATA_DIR/images/musicians/<name>/
     LIBRARY_ROOT/<Artist>/_images          -> DATA_DIR/images/artists/<Artist>/
   The emptied `_images` dir is removed, and an emptied bucket folder
   (_venues/<name>, _events/<name>, _musicians/<name>) is removed too. This
   step is filesystem-only and runs only with --apply; a dry run lists what
   it WOULD move. LIBRARY_ROOT being unreachable (unmounted volume) is
   tolerated -- the step is skipped with a message, not a failure.
3. Assert: recording.kind and both new tables present, row counts unchanged
   for every table this script touches.

No-op on a second run: ALTER/CREATE are all guarded by an existence check, and
the filesystem move only ever acts on an `_images` dir that still exists.

Quit Trellis and take a backup first. Then, from the repo root:
    python3 scripts/migrate_bulk_ingest.py            # dry run
    python3 scripts/migrate_bulk_ingest.py --apply
    python3 scripts/migrate_bulk_ingest.py --db PATH  # other database
"""

import argparse
import os
import shutil
import sqlite3
import sys
import unicodedata
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "db" / "trellis.db"
LIBRARY_ROOT = Path(os.environ.get("LIBRARY_ROOT", "/Volumes/music/Trellis/Library"))

# Bucket folder name -> image_dir() `kind` bucket name (app/utils/entity_images.py).
BUCKETS = {
    "_venues":    "venues",
    "_events":    "events",
    "_musicians": "musicians",
}


def _data_dir():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from config import Config
    return Path(os.environ.get("TRELLIS_DATA_DIR") or Config.DATA_DIR)


def columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info('{table}')")}


def tables(con):
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def add_kind_column(con):
    print("\nColumns")
    if "kind" in columns(con, "recording"):
        print("  recording.kind already present")
        return False
    sql = "ALTER TABLE recording ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'live'"
    print("  " + sql)
    con.execute(sql)
    return True


def create_bulk_ingest_tables(con):
    print("\nTables")
    have = tables(con)
    if "bulk_ingest_run" in have and "bulk_ingest_item" in have:
        print("  bulk_ingest_run / bulk_ingest_item already present")
        return
    con.execute("""
        CREATE TABLE IF NOT EXISTS bulk_ingest_run (
            id INTEGER PRIMARY KEY,
            root VARCHAR(1024) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'running',
            started_at DATETIME NOT NULL,
            finished_at DATETIME,
            last_error TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS ix_bulk_ingest_run_status "
                "ON bulk_ingest_run(status)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS bulk_ingest_item (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES bulk_ingest_run(id) ON DELETE CASCADE,
            rel_path VARCHAR(512) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            reason VARCHAR(32),
            detail TEXT,
            kind VARCHAR(16),
            format VARCHAR(32),
            meta TEXT,
            recording_id INTEGER REFERENCES recording(id) ON DELETE SET NULL,
            duplicate_of INTEGER REFERENCES recording(id) ON DELETE SET NULL,
            updated_at DATETIME NOT NULL,
            UNIQUE(run_id, rel_path)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS ix_bulk_ingest_item_run_id "
                "ON bulk_ingest_item(run_id)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_bulk_ingest_item_status "
                "ON bulk_ingest_item(status)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_bulk_ingest_item_recording_id "
                "ON bulk_ingest_item(recording_id)")
    print("  created bulk_ingest_run, bulk_ingest_item")


def add_format_column(con):
    """
    bulk_ingest_item.format (2026-09-27 progress/log redesign): an install
    that already ran create_bulk_ingest_tables() before this column existed
    gets it added here; a fresh install gets it straight from the CREATE
    TABLE above, so this is a no-op there.
    """
    print("\nColumns")
    if "format" in columns(con, "bulk_ingest_item"):
        print("  bulk_ingest_item.format already present")
        return False
    sql = "ALTER TABLE bulk_ingest_item ADD COLUMN format VARCHAR(32)"
    print("  " + sql)
    con.execute(sql)
    return True


def add_meta_column(con):
    """
    bulk_ingest_item.meta (2026-09-27 progress/log redesign): the extracted
    artist/date/venue/location/source/title fields for the log's expand
    panel, stashed at extraction time so GET .../items never re-scans."""
    print("\nColumns")
    if "meta" in columns(con, "bulk_ingest_item"):
        print("  bulk_ingest_item.meta already present")
        return False
    sql = "ALTER TABLE bulk_ingest_item ADD COLUMN meta TEXT"
    print("  " + sql)
    con.execute(sql)
    return True


def add_folder_path_index(con):
    # N4: process() and the review-reconciliation query both look a
    # Recording up by folder_path per item -- a full table scan that grows
    # with the library. Guarded by IF NOT EXISTS like every other index this
    # script adds, so a second run is a no-op.
    print("\nIndexes")
    con.execute("CREATE INDEX IF NOT EXISTS ix_recording_folder_path "
                "ON recording(folder_path)")
    print("  ensured ix_recording_folder_path")


def _move_images_dir(src, dest, apply, moved_log):
    """Move the contents of one `_images` dir to `dest`, remove the emptied
    source (and its now-empty parent bucket entry, if any). Dry run only logs."""
    files = sorted(p for p in src.iterdir() if p.is_file())
    moved_log.append((src, dest, len(files)))
    if not apply:
        return
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        # Path.rename fails across devices (SMB library, local DATA_DIR);
        # shutil.move falls back to a copy+unlink when os.rename can't do
        # it in place, and is safe to re-run if it dies partway through.
        shutil.move(str(f), str(dest / f.name))
    # Remove the now-empty source dir(s).
    try:
        src.rmdir()
    except OSError:
        return
    parent = src.parent   # the per-entity folder, e.g. _venues/The Fillmore
    try:
        if not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


def migrate_images(apply):
    print("\nImage store move")
    if not LIBRARY_ROOT.is_dir():
        print(f"  LIBRARY_ROOT not reachable ({LIBRARY_ROOT}) -- skipping the move.")
        return []

    data_dir = _data_dir()
    moved = []

    # Bucketed entities: venues, events, musicians.
    for bucket_name, kind in BUCKETS.items():
        bucket_dir = LIBRARY_ROOT / bucket_name
        if not bucket_dir.is_dir():
            continue
        for entity_dir in sorted(p for p in bucket_dir.iterdir() if p.is_dir()):
            images_dir = entity_dir / "_images"
            if images_dir.is_dir():
                name = unicodedata.normalize("NFC", entity_dir.name)
                _move_images_dir(images_dir, data_dir / "images" / kind / name,
                                 apply, moved)

    # Artists: any direct child of LIBRARY_ROOT that is not a bucket / hidden
    # folder and carries an _images dir.
    for entity_dir in sorted(p for p in LIBRARY_ROOT.iterdir() if p.is_dir()):
        if entity_dir.name.startswith(("_", ".")):
            continue
        images_dir = entity_dir / "_images"
        if images_dir.is_dir():
            name = unicodedata.normalize("NFC", entity_dir.name)
            _move_images_dir(images_dir, data_dir / "images" / "artists" / name,
                             apply, moved)

    if not moved:
        print("  Nothing to move.")
    for src, dest, n in moved:
        verb = "Moved" if apply else "Would move"
        print(f"  {verb} {n} file(s): {src} -> {dest}")
    return moved


def assert_post(con, before):
    problems = []
    have_recording = columns(con, "recording")
    if "kind" not in have_recording:
        problems.append("recording missing 'kind'")
    have = tables(con)
    for t in ("bulk_ingest_run", "bulk_ingest_item"):
        if t not in have:
            problems.append(f"missing table {t}")
    if "bulk_ingest_item" in have and "format" not in columns(con, "bulk_ingest_item"):
        problems.append("bulk_ingest_item missing 'format'")
    if "bulk_ingest_item" in have and "meta" not in columns(con, "bulk_ingest_item"):
        problems.append("bulk_ingest_item missing 'meta'")

    for table in ("recording", "artist", "venue", "musician", "event"):
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if n != before[table]:
            problems.append(f"row count changed for {table}: {before[table]} -> {n}")

    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true",
                    help="commit to the database and move files on disk "
                         "(default: dry run, nothing written)")
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"No database at {args.db}")

    if args.apply:
        # Same non-empty-WAL refusal and sqlite3 .backup() snapshot as
        # migrate_file_handling.py -- see that script's comments for why.
        wal_path = args.db.with_name(args.db.name + "-wal")
        if wal_path.exists() and wal_path.stat().st_size > 0:
            sys.exit(f"{wal_path} is non-empty -- quit Trellis first (or checkpoint the WAL) "
                      "before applying this migration.")
        backup_path = args.db.with_name(args.db.name + ".pre-bulk-ingest-migration.bak")
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
              for t in ("recording", "artist", "venue", "musician", "event")}

    con.execute("BEGIN IMMEDIATE")
    try:
        add_kind_column(con)
        create_bulk_ingest_tables(con)
        add_format_column(con)
        add_meta_column(con)
        add_folder_path_index(con)
        problems = assert_post(con, before)
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

    # Filesystem move happens outside the DB transaction (it isn't one), same
    # ordering as every other move in this codebase: DB state settles first.
    migrate_images(apply=args.apply)

    if not args.apply:
        print("\nDry run -- nothing written. Re-run with --apply.")
    con.close()


if __name__ == "__main__":
    main()
