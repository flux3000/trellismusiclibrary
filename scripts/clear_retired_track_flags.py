"""scripts/clear_retired_track_flags.py

Removes the retired track flags 'medley' and 'unknown_title' from track.flags.
Other flags on the same track are kept; a track left with none gets NULL, the
same as the API does.

DRY-RUN by default. --commit takes a backup next to the database, then applies.

    python3 scripts/clear_retired_track_flags.py
    python3 scripts/clear_retired_track_flags.py --commit

Run it where the live database is (the Mac mini). TRELLIS_DB_PATH overrides.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config

RETIRED = {"medley", "unknown_title"}


def main(commit):
    path = str(Config.DB_PATH)
    con = sqlite3.connect(path)
    rows = con.execute(
        "SELECT id, flags FROM track "
        "WHERE flags LIKE '%medley%' OR flags LIKE '%unknown_title%'").fetchall()

    changes = []
    for tid, raw in rows:
        try:
            current = json.loads(raw)
        except (TypeError, ValueError):
            continue
        kept = [f for f in current if f not in RETIRED]
        if kept != current:
            changes.append((tid, json.dumps(kept) if kept else None))

    print(f"Database: {path}")
    print(f"Tracks to update: {len(changes)}")
    if not changes:
        return
    if not commit:
        print("Dry run. Re-run with --commit to apply.")
        return

    backup = f"{path}.pre-flag-cleanup-{datetime.now():%Y%m%d-%H%M%S}"
    dst = sqlite3.connect(backup)
    con.backup(dst)      # WAL-safe, unlike a file copy
    dst.close()
    print(f"Backup: {backup}")

    with con:
        con.executemany("UPDATE track SET flags = ? WHERE id = ?",
                        [(flags, tid) for tid, flags in changes])
    print(f"Updated {len(changes)} track(s).")


if __name__ == "__main__":
    main("--commit" in sys.argv)
