"""
migrate_add_system_collections.py — Add collection.system_key and create the
"Full Library" system collection.

WHY
---
Peer sharing has exactly one primitive: the Collection grant. Whole-library
sharing therefore needs a Collection that MEANS "everything" rather than a
second sharing mechanism with a second authorization path — a second path is
how a leak gets written (Ryan, 2026-08-24).

A system collection is a real Collection row whose membership is resolved by
QUERY rather than by `collection_recording` junction rows. `system_key` is NULL
for every ordinary collection and non-NULL for a dynamic one. One nullable
column rather than a boolean plus a key, because "flagged as system but has no
key" is a state that should not be representable.

Full Library resolves to every recording with is_published = 1. A show moved out
to Workshop or Backlog stops being shared — its folder is no longer under
LIBRARY_ROOT, so it would browse but not play.

⚠ Uniqueness comes from a separate index, not the column definition: SQLite
cannot add a UNIQUE column via ALTER TABLE. The index is what stops a node
ending up with two Full Library rows.

⚠ Ids differ per node. Look the collection up by system_key, never by id.

Additive and idempotent — safe to re-run.

Run once:
    cd ~/Workshop/dev/trellis
    python3 scripts/migrate_add_system_collections.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
from app.models.collection import Collection, SYSTEM_FULL_LIBRARY
import sqlalchemy as sa

FULL_LIBRARY_NAME = "Full Library"
FULL_LIBRARY_DESC = (
    "Everything currently on the shelf. Membership is resolved live, so shows "
    "added later appear automatically and shows moved out to Workshop or "
    "Backlog drop out."
)


def main():
    app = create_app()
    with app.app_context():
        with db.engine.connect() as conn:
            cols = [row[1] for row in conn.execute(
                sa.text("PRAGMA table_info(collection)")
            )]

            if "system_key" not in cols:
                conn.execute(sa.text(
                    "ALTER TABLE collection ADD COLUMN system_key VARCHAR(50)"
                ))
                conn.commit()
                print("Added 'system_key' (VARCHAR(50) NULL) to collection.")
            else:
                print("Column 'system_key' already exists — skipping.")

            # Separate statement: ALTER TABLE ADD COLUMN cannot carry UNIQUE in
            # SQLite. Partial index so the many NULLs don't collide.
            conn.execute(sa.text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_collection_system_key "
                "ON collection (system_key) WHERE system_key IS NOT NULL"
            ))
            conn.commit()
            print("Ensured unique index on collection.system_key.")

        existing = (db.session.query(Collection)
                    .filter_by(system_key=SYSTEM_FULL_LIBRARY).first())
        if existing:
            print(f"'{existing.name}' (id={existing.id}) already exists — "
                  f"nothing to create.")
        else:
            c = Collection(name=FULL_LIBRARY_NAME,
                           description=FULL_LIBRARY_DESC,
                           system_key=SYSTEM_FULL_LIBRARY)
            db.session.add(c)
            db.session.commit()
            print(f"Created '{c.name}' (id={c.id}, system_key="
                  f"'{SYSTEM_FULL_LIBRARY}').")

        c = (db.session.query(Collection)
             .filter_by(system_key=SYSTEM_FULL_LIBRARY).first())
        print(f"Full Library currently resolves to {c.recording_count} "
              f"published recordings.")


if __name__ == "__main__":
    main()
