"""
migrate_add_is_published.py — Add is_published column to recording table.

is_published: NOT NULL boolean, default 1. True means the recording's folder
lives in the library and the recording is part of the browsable collection.
False means it has been moved back out to Workshop or Backlog — the audio still
exists, the library record still exists, but the show is off the shelf.

It exists because "Move to Workshop/Backlog" on an ingested recording had no
honest state to write (Ryan, 2026-08-21). Deleting the row loses the metadata,
lineage, checksums and history that ingest produced; leaving the row published
with its folder gone gives a show that browses and will not play.

Existing rows all backfill to 1: everything already in the database is, by
definition, in the library.

Additive and idempotent — safe to re-run.

Run once:
    cd ~/Workshop/dev/trellis
    python3 scripts/migrate_add_is_published.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
import sqlalchemy as sa


def main():
    app = create_app()
    with app.app_context():
        with db.engine.connect() as conn:
            cols = [row[1] for row in conn.execute(
                sa.text("PRAGMA table_info(recording)")
            )]
            if "is_published" in cols:
                print("Column 'is_published' already exists — nothing to do.")
                return

            # NOT NULL with a server default so existing rows backfill to 1
            # rather than the ALTER failing on a non-null column with no default.
            conn.execute(sa.text(
                "ALTER TABLE recording ADD COLUMN is_published "
                "BOOLEAN NOT NULL DEFAULT 1"
            ))
            conn.commit()
            n = conn.execute(sa.text("SELECT COUNT(*) FROM recording")).scalar()
            print(f"Added 'is_published' (BOOLEAN NOT NULL DEFAULT 1); "
                  f"{n} existing recordings marked as in-library.")


if __name__ == "__main__":
    main()
