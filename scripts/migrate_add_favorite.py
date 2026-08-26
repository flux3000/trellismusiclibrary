"""
migrate_add_favorite.py — Add is_favorite column to recording table.

is_favorite: NOT NULL boolean, default 0. A one-click human "highlight" marker,
independent of both `quality` (the letter grade) and the automated Listening
Quality score. See the model comment on Recording.is_favorite for why it is a
boolean rather than a star scale.

Additive and idempotent — safe to re-run.

Run once:
    cd ~/Workshop/dev/trellis
    python3 scripts/migrate_add_favorite.py
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
            if "is_favorite" in cols:
                print("Column 'is_favorite' already exists — nothing to do.")
                return

            # NOT NULL with a server default so existing rows backfill to 0
            # rather than the ALTER failing on a non-null column with no default.
            conn.execute(sa.text(
                "ALTER TABLE recording ADD COLUMN is_favorite "
                "BOOLEAN NOT NULL DEFAULT 0"
            ))
            conn.commit()
            n = conn.execute(sa.text("SELECT COUNT(*) FROM recording")).scalar()
            print(f"Added 'is_favorite' (BOOLEAN NOT NULL DEFAULT 0); "
                  f"{n} existing recordings set to not-favorite.")


if __name__ == "__main__":
    main()
