#!/usr/bin/env python3
"""fix_windows_names.py — rename existing library files so Windows can hold them.

  Tweezer>Light>Down with Disease   ->  Tweezer - Light - Down with Disease
  Encore: Cheeseballs In Cowtown    ->  Encore - Cheeseballs In Cowtown
  Theme from M*A*S*H*               ->  Theme from MASH
  Chicken Grease > "What It Do"     ->  Chicken Grease - 'What It Do'
  over-long medley names            ->  cut at a word, path <= 200 characters

Only the name on disk changes; titles in the database are untouched. A
library's own naming is kept (no naming scheme is applied).

Safety:
  * dry run by default; --apply is required to touch anything
  * refuses to run while Trellis is open
  * the database is backed up (sqlite .backup, WAL-safe) before any write
  * each recording is all-or-nothing: a failed rename puts that recording's
    files back and rolls its rows back
  * every rename is logged to a CSV beside the database, for undoing by hand

Run it on the machine that owns the library (the Mac mini), Trellis quit:
    python3 tools/fix_windows_names.py             # report
    python3 tools/fix_windows_names.py --apply     # do it
"""

import argparse
import csv
import importlib.util
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path.home() / "Library/Application Support/Trellis Music Library/db/trellis.db"
DEFAULT_LIB = "/Volumes/music/Trellis/Library"

# Load the naming engine by file path: importing the app package would pull in
# Flask, and this script needs only the pure functions.
_spec = importlib.util.spec_from_file_location("file_naming", REPO / "app/utils/file_naming.py")
fn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fn)


def trellis_running():
    r = subprocess.run(["pgrep", "-f", "Trellis Music Library|run.py|run_headless.py"],
                       capture_output=True, text=True)
    return [p for p in r.stdout.split() if p and int(p) != os.getpid()]


def plan(conn):
    """{recording_id: (folder_path, [(track_id, old_rel, new_rel), ...])}"""
    tracks = defaultdict(list)
    folders = {}
    rows = conn.execute(
        "SELECT r.id, r.folder_path, t.id, t.file_path FROM track t "
        "JOIN recording r ON r.id = t.recording_id "
        "WHERE r.folder_path IS NOT NULL ORDER BY r.id, t.track_number, t.id")
    for rid, folder, tid, fp in rows:
        folders[rid] = folder
        tracks[rid].append((tid, fp or ""))
    out = {}
    for rid, items in tracks.items():
        new = fn.safe_name_plan(folders[rid], [fp for _, fp in items])
        changes = [(tid, old, nw) for (tid, old), nw in zip(items, new) if old != nw]
        if changes:
            out[rid] = (folders[rid], changes)
    return out, folders


def unsafe_folders(folders):
    return sorted(f for f in folders.values()
                  if any(fn.windows_safe(p) != p for p in f.split("/")))


def apply_recording(conn, lib, folder, changes, log):
    """Rename one recording's files and rows together; undo both on failure."""
    base = Path(lib) / folder
    done = []
    try:
        for tid, old, new in changes:
            src, dst = base / old, base / new
            if not src.exists():
                raise OSError(f"missing on disk: {src}")
            # Case-only difference is the same file on macOS; anything else
            # already at the destination is someone else's file.
            if dst.exists() and not os.path.samefile(src, dst):
                raise OSError(f"destination already exists: {dst}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)
            done.append((src, dst))
            conn.execute("UPDATE track SET file_path = ? WHERE id = ?", (new, tid))
        conn.commit()
    except Exception:
        conn.rollback()
        for src, dst in reversed(done):
            try:
                os.rename(dst, src)
            except OSError as e:
                print(f"    !! could not put back {dst} -> {src}: {e}")
        raise
    for tid, old, new in changes:
        log.writerow([folder, tid, old, new])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--library", default=DEFAULT_LIB)
    ap.add_argument("--apply", action="store_true", help="rename files (default: report only)")
    args = ap.parse_args()

    db, lib = Path(args.db), Path(args.library)
    if not db.exists():
        sys.exit(f"No database at {db}. Pass --db.")
    if not lib.is_dir():
        sys.exit(f"Library folder {lib} is not reachable. Is the share mounted? Pass --library.")
    if args.apply and trellis_running():
        sys.exit("Trellis is running. Quit it first, then run again.")

    conn = sqlite3.connect(f"file:{db}?mode={'rw' if args.apply else 'ro'}", uri=True)
    changes, folders = plan(conn)
    n_tracks = sum(len(c) for _, c in changes.values())

    for rid, (folder, items) in sorted(changes.items()):
        print(f"\n{folder}")
        for _, old, new in items:
            print(f"    {old}\n -> {new}")
    bad = unsafe_folders(folders)
    print(f"\n{n_tracks} files in {len(changes)} recordings to rename.")
    if bad:
        print(f"{len(bad)} recording folders also have Windows-unsafe names; this "
              f"script does not rename folders:")
        for f in bad[:20]:
            print(f"    {f}")

    if not args.apply:
        print("\nDry run. Nothing changed. Run again with --apply to rename.")
        return 0
    if not changes:
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db.with_name(f"{db.stem}.pre-windows-names-{stamp}.db")
    with sqlite3.connect(str(backup)) as dst:
        conn.backup(dst)
    print(f"\nDatabase backed up to {backup}")

    log_path = db.with_name(f"windows-names-{stamp}.csv")
    ok = failed = 0
    with open(log_path, "w", newline="") as fh:
        log = csv.writer(fh)
        log.writerow(["folder_path", "track_id", "old_file_path", "new_file_path"])
        for rid, (folder, items) in sorted(changes.items()):
            try:
                apply_recording(conn, lib, folder, items, log)
                ok += len(items)
            except Exception as e:
                failed += 1
                print(f"  ✗ {folder}: {e} (left as it was)")
    print(f"\nRenamed {ok} files. {failed} recordings skipped. Log: {log_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
