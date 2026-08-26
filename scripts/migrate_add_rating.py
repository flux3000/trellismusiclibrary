"""
migrate_add_rating.py — Add rating column to recording table.

rating: nullable integer 0–100. Holistic listener score (show quality +
experience). Separate from `quality` (technical recording grade letter).

Run once:
    cd ~/Workshop/dev/trellis
    python3 scripts/migrate_add_rating.py
"""

import sys
from pathlib import Path

# Make sure app is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
import sqlalchemy as sa


def main():
    app = create_app()
    with app.app_context():
        with db.engine.connect() as conn:
            # Check if column already exists
            cols = [row[1] for row in conn.execute(
                sa.text("PRAGMA table_info(recording)")
            )]
            if "rating" in cols:
                print("Column 'rating' already exists — nothing to do.")
                return

            conn.execute(sa.text(
                "ALTER TABLE recording ADD COLUMN rating INTEGER"
            ))
            conn.commit()
            print("Added column 'rating' (INTEGER, nullable) to recording table.")


if __name__ == "__main__":
    main()
