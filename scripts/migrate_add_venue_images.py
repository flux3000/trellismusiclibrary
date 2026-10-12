"""scripts/migrate_add_venue_images.py

Adds `venue_image` — multiple photos per Venue with one flagged primary.
Additive and idempotent.

A parallel table to `performer_image` rather than a shared polymorphic one. The
duplication is schema only: behaviour lives once in
`app/utils/entity_images.py`. The reason is the foreign key — FK enforcement
was turned on deliberately in July, and SQLite cannot enforce a polymorphic
(entity_type, entity_id) pair at all.

Creates no rows. Venue photos start empty and are uploaded by hand; there is no
Commons fetch path for venues (Wikidata's P18 is an artist/place image but the
MusicBrainz → Wikidata bridge we use runs through the Performer, not the Venue).

Touches neither `performance` nor `user_artist_permission`, so the hardcoded DDL
snapshots in tools/repair_stale_fk_ddl.py need no re-dump.

    python3 scripts/migrate_add_venue_images.py
    python3 scripts/migrate_add_venue_images.py --dry-run
"""

import os
import sys
import sqlite3

DB = os.environ.get(
    "FLUX_DB",
    os.path.join(os.path.dirname(__file__), "..", "db", "fluxaudio.db"),
)


def main():
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(DB)
    cur = con.cursor()

    print(f"DB: {os.path.abspath(DB)}")
    print(f"Mode: {'DRY RUN' if dry else 'write'}\n")

    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    if "venue" not in tables:
        print("!! venue table missing — wrong database?")
        return 1

    if "venue_image" in tables:
        print("venue_image already exists — nothing to do.")
    else:
        print("+ CREATE TABLE venue_image")
        if not dry:
            cur.execute("""
                CREATE TABLE venue_image (
                    id         INTEGER PRIMARY KEY,
                    venue_id   INTEGER NOT NULL
                               REFERENCES venue(id) ON DELETE CASCADE,
                    filename   VARCHAR(255) NOT NULL,
                    ext        VARCHAR(8)   NOT NULL,
                    is_primary BOOLEAN NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    origin     VARCHAR(24) NOT NULL DEFAULT 'upload',
                    caption    VARCHAR(255),
                    credit     VARCHAR(255),
                    source_ref VARCHAR(255),
                    created_at DATETIME
                )""")
            cur.execute("CREATE INDEX ix_venue_image_venue_id "
                        "ON venue_image(venue_id)")
            con.commit()

    con.close()
    print("\nDone." if not dry else "\nDry run — nothing written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
