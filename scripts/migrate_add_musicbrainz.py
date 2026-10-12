"""scripts/migrate_add_musicbrainz.py

Adds the MusicBrainz fact columns to `performer`. Additive and idempotent —
safe to re-run.

Real columns rather than a JSON blob: these are queryable facts ("which acts
formed in New Orleans"), and a blob can't answer that.

Every column is nullable and every existing row keeps `mb_status = NULL`, which
the model documents as "never looked up" — distinct from 'none' ("looked, found
nothing"). That distinction is load-bearing: the performer page offers a Match
prompt for NULL and 'ambiguous', and stays quiet for 'matched' and 'none'.
Backfilling existing performers is a separate, deliberate act (a lookup per
performer at ~1.1s apiece for 164 acts), not something a schema migration
should do behind your back.

Touches neither `performance` nor `user_artist_permission`, so the hardcoded
DDL snapshots in tools/repair_stale_fk_ddl.py need no re-dump.

    python3 scripts/migrate_add_musicbrainz.py
    python3 scripts/migrate_add_musicbrainz.py --dry-run
"""

import os
import sys
import sqlite3

DB = os.environ.get(
    "FLUX_DB",
    os.path.join(os.path.dirname(__file__), "..", "db", "fluxaudio.db"),
)

COLUMNS = [
    ("mbid",              "VARCHAR(36)"),
    ("mb_status",         "VARCHAR(16)"),
    ("mb_type",           "VARCHAR(32)"),
    ("mb_area",           "VARCHAR(120)"),
    ("mb_begin",          "VARCHAR(10)"),
    ("mb_end",            "VARCHAR(10)"),
    ("mb_disambiguation", "VARCHAR(255)"),
    ("mb_links_json",     "TEXT"),
    # Lists (aliases, tags, related acts) + gender. JSON because they are
    # lists — the real-columns rule covers queryable scalars, not arrays.
    ("mb_extra_json",     "TEXT"),
    ("mb_checked_at",     "DATETIME"),
]


def main():
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(DB)
    cur = con.cursor()

    print(f"DB: {os.path.abspath(DB)}")
    print(f"Mode: {'DRY RUN' if dry else 'write'}\n")

    existing = {r[1] for r in cur.execute("PRAGMA table_info(performer)")}
    added = 0
    for name, decl in COLUMNS:
        if name in existing:
            print(f"  · performer.{name} already exists")
            continue
        print(f"  + ALTER TABLE performer ADD COLUMN {name} {decl}")
        if not dry:
            cur.execute(f"ALTER TABLE performer ADD COLUMN {name} {decl}")
        added += 1

    # Indexed because every future re-lookup and any Commons/Wikidata image
    # fetch keys off the MBID, not the name — that's the whole point of storing
    # a stable id rather than re-searching a string each time.
    idx = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    if "ix_performer_mbid" in idx:
        print("  · ix_performer_mbid already exists")
    else:
        print("  + CREATE INDEX ix_performer_mbid")
        if not dry:
            cur.execute("CREATE INDEX ix_performer_mbid ON performer(mbid)")

    if not dry:
        con.commit()
    con.close()

    print(f"\n{added} column(s) added." if not dry else "\nDry run — nothing written.")
    print("Existing performers keep mb_status = NULL ('never looked up').")
    print("Backfill them from the app, or with:")
    print("    python3 scripts/backfill_musicbrainz.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
