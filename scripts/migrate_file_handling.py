"""
scripts/migrate_file_handling.py -- 2026-09-25 file handling + disc/set split.

Same shape as scripts/migrate_rename_artist_musician.py: dry run by default,
one transaction, assertions before commit.

1. ALTER TABLE for the seven new columns:
     track.disc_number, track.disc_track_number, track.original_file_path
     recording_fingerprint.rel_path
     recording.source_tag, recording.etree_shnid
     artist.abbreviation
2. Seed file_handling_mode=organize and organize's switch defaults into
   node_setting when the database already has recording rows -- an existing
   library already looks organized; a fresh one has not chosen yet.
3. Delete user_preference rows with key ingest_file_behavior (retired --
   copy ingest is gone, section 3.3 of the spec).
4. Disc migration: every track whose set_number matches ^(Disc|CD) (\\d+)$
   gets disc_number set to the digit, disc_track_number set to its 1-based
   order by track_number within (recording_id, disc_number), and set_number
   cleared. Any other non-null set_number is left alone and logged -- it is a
   musical set, a different family entirely.
5. Backfill recording_fingerprint.rel_path from discover_fingerprint_files()
   for every recording whose folder is reachable under LIBRARY_ROOT; left
   NULL where the folder cannot be reached (offline volume, moved recording).
6. Assert: all seven columns present, row counts unchanged, no set_number
   matching the disc pattern remains.

Quit Trellis and take a backup first. Then, from the repo root:
    python3 scripts/migrate_file_handling.py            # dry run
    python3 scripts/migrate_file_handling.py --apply
    python3 scripts/migrate_file_handling.py --db PATH  # other database
"""

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "db" / "trellis.db"
LIBRARY_ROOT = Path(os.environ.get("LIBRARY_ROOT", "/Volumes/music/Trellis/Library"))

NEW_COLUMNS = [
    ("track", "disc_number", "INTEGER"),
    ("track", "disc_track_number", "INTEGER"),
    ("track", "original_file_path", "VARCHAR(512)"),
    ("recording_fingerprint", "rel_path", "VARCHAR(512)"),
    ("recording", "source_tag", "VARCHAR(64)"),
    ("recording", "etree_shnid", "INTEGER"),
    ("artist", "abbreviation", "VARCHAR(16)"),
]

ORGANIZE_SWITCHES = {
    "rename_folders": "true",
    "rename_files": "true",
    "naming_scheme": "number_title",
    "write_tags_on_ingest": "true",
    "write_tags_default": "true",
}

DISC_SET_RE = re.compile(r"^(Disc|CD) (\d+)$")


def columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info('{table}')")}


def add_columns(con):
    print("\nColumns")
    added = []
    for table, name, coltype in NEW_COLUMNS:
        if name in columns(con, table):
            continue
        sql = f"ALTER TABLE {table} ADD COLUMN {name} {coltype}"
        print("  " + sql)
        con.execute(sql)
        added.append((table, name))
    return added


def seed_file_handling_mode(con):
    """
    Seed file_handling_mode=organize (plus its switches) ONLY when the key
    is absent -- a fresh database that already looks organized. S11
    (re-review, 2026-09-25): this used to ON CONFLICT DO UPDATE, so a second
    --apply against a database where the OWNER had since chosen 'keep' (or
    hand-tuned an individual switch) silently flipped it back to organize's
    defaults on every re-run. ON CONFLICT DO NOTHING makes this a true
    no-op once the key exists, whatever value it holds.
    """
    n_recordings = con.execute("SELECT count(*) FROM recording").fetchone()[0]
    if n_recordings == 0:
        print("\nNo recordings -- file_handling_mode left unset (reads as 'keep').")
        return
    existing = con.execute(
        "SELECT value FROM node_setting WHERE key='file_handling_mode'"
    ).fetchone()
    if existing:
        print(f"\nfile_handling_mode already set ({existing[0]!r}) -- left as is.")
        return
    print(f"\n{n_recordings} recording(s) present -- seeding file_handling_mode=organize")
    con.execute(
        "INSERT INTO node_setting (key, value) VALUES ('file_handling_mode', 'organize') "
        "ON CONFLICT(key) DO NOTHING"
    )
    for key, value in ORGANIZE_SWITCHES.items():
        con.execute(
            "INSERT INTO node_setting (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO NOTHING",
            (key, value),
        )


def drop_ingest_file_behavior(con):
    n = con.execute(
        "SELECT count(*) FROM user_preference WHERE key='ingest_file_behavior'"
    ).fetchone()[0]
    if n:
        print(f"\nDeleting {n} user_preference row(s) with key='ingest_file_behavior'")
        con.execute("DELETE FROM user_preference WHERE key='ingest_file_behavior'")


def migrate_discs(con):
    print("\nDisc migration")
    rows = con.execute(
        "SELECT id, recording_id, track_number, set_number FROM track "
        "WHERE set_number IS NOT NULL"
    ).fetchall()

    disc_rows = []
    other_non_null = []
    for row_id, recording_id, track_number, set_number in rows:
        m = DISC_SET_RE.match(set_number)
        if m:
            disc_rows.append((row_id, recording_id, track_number, int(m.group(2))))
        else:
            other_non_null.append((row_id, recording_id, set_number))

    if other_non_null:
        print(f"  Leaving {len(other_non_null)} non-disc set_number row(s) untouched:")
        for row_id, recording_id, set_number in other_non_null[:10]:
            print(f"    track {row_id} (recording {recording_id}): {set_number!r}")
        if len(other_non_null) > 10:
            print(f"    ... and {len(other_non_null) - 10} more")

    by_disc = {}
    for row_id, recording_id, track_number, disc_number in disc_rows:
        by_disc.setdefault((recording_id, disc_number), []).append((track_number, row_id))

    touched_recordings = set()
    for (recording_id, disc_number), members in by_disc.items():
        touched_recordings.add(recording_id)
        for disc_track_number, (_track_number, row_id) in enumerate(
            sorted(members, key=lambda pair: pair[0]), start=1
        ):
            con.execute(
                "UPDATE track SET disc_number=?, disc_track_number=?, set_number=NULL "
                "WHERE id=?",
                (disc_number, disc_track_number, row_id),
            )

    print(f"  {len(disc_rows)} row(s) in {len(touched_recordings)} recording(s) migrated "
          f"from set_number to disc_number/disc_track_number")
    return len(disc_rows), len(touched_recordings)


def backfill_fingerprint_rel_paths(con):
    print("\nFingerprint rel_path backfill")
    if not LIBRARY_ROOT.is_dir():
        print(f"  LIBRARY_ROOT not reachable ({LIBRARY_ROOT}) -- leaving all rel_path NULL.")
        return

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.utils.checksums import discover_fingerprint_files

    recordings = con.execute(
        "SELECT id, folder_path FROM recording WHERE is_published=1"
    ).fetchall()
    filled = 0
    for recording_id, folder_path in recordings:
        folder_abs = LIBRARY_ROOT / folder_path
        if not folder_abs.is_dir():
            continue
        found = discover_fingerprint_files(str(folder_abs))
        if not found:
            continue
        by_filename = {f["filename"]: f["rel_path"] for f in found}
        fp_rows = con.execute(
            "SELECT id, filename FROM recording_fingerprint "
            "WHERE recording_id=? AND rel_path IS NULL",
            (recording_id,),
        ).fetchall()
        for fp_id, filename in fp_rows:
            rel_path = by_filename.get(filename)
            if rel_path:
                con.execute(
                    "UPDATE recording_fingerprint SET rel_path=? WHERE id=?",
                    (rel_path, fp_id),
                )
                filled += 1
    print(f"  {filled} recording_fingerprint row(s) backfilled")


def assert_post(con, before, added_columns):
    problems = []

    have_track = columns(con, "track")
    have_fp = columns(con, "recording_fingerprint")
    have_recording = columns(con, "recording")
    have_artist = columns(con, "artist")

    for must, have, table in (
        ({"disc_number", "disc_track_number", "original_file_path"}, have_track, "track"),
        ({"rel_path"}, have_fp, "recording_fingerprint"),
        ({"source_tag", "etree_shnid"}, have_recording, "recording"),
        ({"abbreviation"}, have_artist, "artist"),
    ):
        missing = must - have
        if missing:
            problems.append(f"{table} missing {sorted(missing)}")

    for table in ("track", "recording", "artist"):
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if n != before[table]:
            problems.append(f"row count changed for {table}: {before[table]} -> {n}")

    remaining = con.execute(
        "SELECT count(*) FROM track WHERE set_number LIKE 'Disc %' OR set_number LIKE 'CD %'"
    ).fetchone()[0]
    # Only the exact `^(Disc|CD) (\d+)$` shape is guaranteed migrated away;
    # anything else matching the LIKE but not the regex is a false positive
    # this check would wrongly flag, so re-test with the real pattern.
    if remaining:
        leftover = con.execute(
            "SELECT set_number FROM track WHERE set_number LIKE 'Disc %' OR set_number LIKE 'CD %'"
        ).fetchall()
        if any(DISC_SET_RE.match(s) for (s,) in leftover):
            problems.append("a set_number matching the disc pattern still remains")

    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="commit to the database (default: dry run in memory)")
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"No database at {args.db}")

    if args.apply:
        # S11 (review, 2026-09-25): --apply used to open the live database
        # with no check for a running app and no backup, on a script that
        # rewrites 112 track rows and deletes a preference. A non-empty -wal
        # sidecar means SQLite has uncommitted writes sitting outside the
        # main file -- most likely Trellis itself still running -- so refuse
        # rather than risk applying against a half-written page cache.
        wal_path = args.db.with_name(args.db.name + "-wal")
        if wal_path.exists() and wal_path.stat().st_size > 0:
            sys.exit(f"{wal_path} is non-empty -- quit Trellis first (or checkpoint the WAL) "
                      "before applying this migration.")
        # sqlite3 .backup semantics (S11, re-review 2026-09-25), not a raw
        # file copy: a plain shutil.copy2 against a live SQLite file can
        # read it mid-write (a torn page) even with the WAL check above --
        # Connection.backup() uses SQLite's own online backup API, which
        # takes its own read lock and copies a transactionally consistent
        # snapshot page by page.
        backup_path = args.db.with_name(args.db.name + ".pre-file-handling-migration.bak")
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

    # Precondition dropped (S11): every step below is already idempotent
    # (ON CONFLICT upserts, a plain DELETE, and a regex that only ever
    # matches what it hasn't already cleared), so a second run against an
    # already-migrated database is a no-op, not a refusal.
    before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("track", "recording", "artist")}

    con.execute("BEGIN IMMEDIATE")
    try:
        added_columns = add_columns(con)
        seed_file_handling_mode(con)
        drop_ingest_file_behavior(con)
        migrate_discs(con)
        backfill_fingerprint_rel_paths(con)
        problems = assert_post(con, before, added_columns)
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
        print("\nAll assertions passed. Dry run -- nothing written. Re-run with --apply.")
    con.close()


if __name__ == "__main__":
    main()
