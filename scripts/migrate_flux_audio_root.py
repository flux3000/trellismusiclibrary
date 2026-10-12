"""Repoint stored absolute paths after the Workshop/Library consolidation:

    /Volumes/music/Flux Workshop/{Backlog,Download,Working}  ->  /Volumes/music/Flux Audio/{Backlog,Download,Workshop}
    /Volumes/music/Flux Library                              ->  /Volumes/music/Flux Audio/Library

Only `quality_analysis` stores absolute paths (folder_path, source_dir).
Recording.folder_path and Track.file_path are relative to LIBRARY_ROOT and are
deliberately left alone — that is why the rename is cheap.

Idempotent: rows already pointing at the new root are skipped. Dry-run by
default; pass --commit to write. Takes a timestamped .bak of the DB first.

Usage:
    python3 scripts/migrate_flux_audio_root.py [--db db/fluxaudio.db] [--commit]
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

# Longest-prefix first so "Flux Workshop/Working" is not shadowed by a
# hypothetical shorter rule. Order matters.
REWRITES = [
    ("/Volumes/music/Flux Workshop/Working",  "/Volumes/music/Flux Audio/Workshop"),
    ("/Volumes/music/Flux Workshop/Backlog",  "/Volumes/music/Flux Audio/Backlog"),
    ("/Volumes/music/Flux Workshop/Download", "/Volumes/music/Flux Audio/Download"),
    ("/Volumes/music/Flux Workshop/Training", "/Volumes/music/Flux Audio/Training"),
    ("/Volumes/music/Flux Library",           "/Volumes/music/Flux Audio/Library"),
]

# Deliberately NOT rewritten: "/Volumes/music/Flux Workshop/Import Queue".
# That folder was the landing directory before Download replaced it. The five
# rows referencing it are all promoted (recording_id set), so the path is a
# historical breadcrumb of where the material actually came from. Rewriting it
# would invent a path that never existed on disk. Left as-is on purpose.

TARGETS = [("quality_analysis", "folder_path"), ("quality_analysis", "source_dir")]


def rewrite(value):
    """Apply the first matching prefix rule. Returns None if nothing matched."""
    if not value:
        return None
    for old, new in REWRITES:
        if value.startswith(old):
            return new + value[len(old):]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="db/fluxaudio.db")
    ap.add_argument("--commit", action="store_true", help="write changes")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"no such database: {args.db}")

    if args.commit:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = f"{args.db}.pre-flux-audio-root-{stamp}.bak"
        shutil.copy2(args.db, bak)
        print(f"backup: {bak}")

    db = sqlite3.connect(args.db)
    cur = db.cursor()
    total = 0

    for table, col in TARGETS:
        # Skip cleanly if this DB predates the table (e.g. the peer node).
        exists = cur.execute(
            "select 1 from sqlite_master where type='table' and name=?", (table,)
        ).fetchone()
        if not exists:
            print(f"{table}: absent, skipped")
            continue

        rows = cur.execute(
            f"select rowid, \"{col}\" from {table} where \"{col}\" is not null"
        ).fetchall()
        changed = [(rid, new) for rid, val in rows if (new := rewrite(val))]

        print(f"{table}.{col}: {len(changed)} of {len(rows)} rows to rewrite")
        for rid, new in changed[:3]:
            print(f"    -> {new}")
        if len(changed) > 3:
            print(f"    ... and {len(changed) - 3} more")

        if args.commit and changed:
            cur.executemany(
                f"update {table} set \"{col}\"=? where rowid=?",
                [(new, rid) for rid, new in changed],
            )
        total += len(changed)

    if args.commit:
        db.commit()
        print(f"\ncommitted {total} row updates")
    else:
        print(f"\nDRY RUN — {total} rows would change. Re-run with --commit.")
    db.close()


if __name__ == "__main__":
    main()
