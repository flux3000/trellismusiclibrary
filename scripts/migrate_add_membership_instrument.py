"""
scripts/migrate_add_membership_instrument.py — instrument on Membership.

Adds membership.instrument (nullable VARCHAR(128)), additive and idempotent.
Run this AFTER pulling the matching model change in app/models/artist.py
(Membership.instrument) — this script only touches the schema; the ORM
column declaration is a separate, already-applied source edit.

Context: doodah.net/bgb roster ingestion (2026-08-25) is the first Membership
data with real stint dates in this app, and the site's richest field —
instrument(s) played — had nowhere to land. `PerformancePersonnel` already has
exactly this column for show-level personnel; this mirrors it one layer up,
at the act-roster level.

One column holds every instrument a person is on record playing during that
STINT (comma-separated, e.g. "fiddle, banjo, bass, mandolin"), because the
source data itself is an aggregate over the tenure, not a per-appearance
breakdown — see the ingest script's docstring for why that's a real limit of
the source, not something lost in translation.

Run once from the repo root:
    python3 scripts/migrate_add_membership_instrument.py
"""

import os
import sqlite3

DB = os.environ.get(
    "FLUX_DB",
    os.path.join(os.path.dirname(__file__), "..", "db", "fluxaudio.db"),
)


def _existing_columns(cur, table):
    return {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()

    if "instrument" in _existing_columns(cur, "membership"):
        print("membership.instrument already exists — nothing to do.")
    else:
        cur.execute("ALTER TABLE membership ADD COLUMN instrument VARCHAR(128)")
        print("added membership.instrument")

    con.commit()
    con.close()
    print("done")


if __name__ == "__main__":
    main()
