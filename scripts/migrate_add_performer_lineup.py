"""
scripts/migrate_add_performer_lineup.py — performer.lineup_json.

Adds performer.lineup_json (nullable TEXT), additive and idempotent. Run this
AFTER pulling the matching model change in app/models/performer.py
(Performer.lineup_json) — this script only touches the schema.

Context: AI lineup research (2026-09-07) returns a roster with per-person stint
dates for human review. The first build held it in page state only, so running
it and navigating away threw away work the human had paid tokens for. It gets
its own column rather than sharing dossier_json, because the biography pass and
the lineup pass are separate actions with separate output shapes and one blob
would mean each run destroying the other's result.

Run once from the repo root:
    python3 scripts/migrate_add_performer_lineup.py
"""

import os
import sqlite3

DB = os.environ.get(
    "FLUX_DB",
    os.path.join(os.path.dirname(__file__), "..", "db", "trellis.db"),
)


def _existing_columns(cur, table):
    return {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()

    if "lineup_json" in _existing_columns(cur, "performer"):
        print("performer.lineup_json already exists — nothing to do.")
    else:
        cur.execute("ALTER TABLE performer ADD COLUMN lineup_json TEXT")
        print("added performer.lineup_json")

    con.commit()
    con.close()
    print("done")


if __name__ == "__main__":
    main()
