#!/usr/bin/env python3
"""
rename_set_column.py — one-time migration: track.set -> track.set_number.

`set` is a SQL reserved word. The ORM (SQLAlchemy) already quotes it safely
in every query it generates, so nothing in the running app was ever broken
by this. But tools/repair_flatten.py wrote raw SQL against the same column
and hit the reserved word directly: an UPDATE ... SET "set"=? threw a syntax
error, and because that script moved files before running the DB update,
the failure briefly left a recording's files moved with the DB rows still
pointing at the old path (caught in testing, never happened to a real
recording — see repair_flatten.py's docstring).

Decision (Ryan, 2026-07-28): rename the column to set_number so no future
raw-SQL script can hit this class of bug again. The app code
(app/models/track.py, the tracks/recordings/share API routes, the ingest
pipeline, and the frontend) has already been updated to use set_number
throughout — this script is only the one physical ALTER on the live
database, which SQLAlchemy's model can't do for you.

Tested against a copy of the real database first: ALTER TABLE RENAME COLUMN
preserved every row's value untouched (9011 tracks total, 28 with a set
label set, before and after).

Usage:
    python3 rename_set_column.py                # dry run — just reports
    python3 rename_set_column.py --apply         # do it (backs up DB first)
"""
import os
import sys
import shutil
import sqlite3
import argparse
from datetime import datetime

DEFAULT_DB = os.path.expanduser("~/Workshop/dev/trellis/db/trellis.db")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="actually make the change")
    a = ap.parse_args()

    if not os.path.isfile(a.db):
        sys.exit(f"database not found: {a.db}")

    con = sqlite3.connect(a.db)
    schema = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='track'").fetchone()
    if not schema:
        con.close()
        sys.exit("no 'track' table found — wrong database?")
    schema_sql = schema[0]

    has_set = '"set"' in schema_sql or " set " in schema_sql
    has_set_number = "set_number" in schema_sql

    if has_set_number and not has_set:
        con.close()
        print("Already renamed — track.set_number exists, nothing to do.")
        return
    if not has_set:
        con.close()
        sys.exit("track.set column not found — schema looks different than expected, stopping.")

    total = con.execute("SELECT COUNT(*) FROM track").fetchone()[0]
    labeled = con.execute('SELECT COUNT(*) FROM track WHERE "set" IS NOT NULL').fetchone()[0]
    print(f"{total} tracks total, {labeled} with a set label — will be preserved.")

    if not a.apply:
        print("\nDry run. Re-run with --apply to rename the column for real.")
        con.close()
        return

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{a.db}.pre-set-rename-{stamp}.bak"
    shutil.copy2(a.db, backup)
    print(f"database backed up to {backup}")

    con.execute('ALTER TABLE track RENAME COLUMN "set" TO set_number')
    con.commit()

    # Verify nothing moved
    total_after = con.execute("SELECT COUNT(*) FROM track").fetchone()[0]
    labeled_after = con.execute(
        "SELECT COUNT(*) FROM track WHERE set_number IS NOT NULL").fetchone()[0]
    con.close()

    if total_after != total or labeled_after != labeled:
        sys.exit(f"MISMATCH after rename — before: {total}/{labeled}, "
                 f"after: {total_after}/{labeled_after}. Restore from {backup}.")

    print(f"Done. track.set_number now holds all {labeled_after} set labels.")


if __name__ == "__main__":
    main()
