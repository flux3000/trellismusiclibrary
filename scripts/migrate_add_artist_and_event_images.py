"""
migrate_add_artist_and_event_images.py — Add the artist_image and event_image
tables.

Photos were performer-and-venue-only until 2026-09-01. Standardising the photo
treatment across all four photographed entities (Ryan) needs one parallel table
each, matching performer_image and venue_image column for column — see
app/models/artist_image.py for why parallel rather than polymorphic (short
version: SQLite cannot enforce an (entity_type, entity_id) foreign key at all,
and FK enforcement was turned on deliberately in July).

Nothing to backfill: no artist or event photo has ever existed. Files will land
under LIBRARY_ROOT/_artists/… and LIBRARY_ROOT/_events/… as they are uploaded.

Additive and idempotent — safe to re-run.

    python3 scripts/migrate_add_artist_and_event_images.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
from app.models.artist_image import ArtistImage
from app.models.event_image import EventImage
from config import Config
import sqlalchemy as sa


def main():
    app = create_app()
    with app.app_context():
        print(f"database: {Config.DB_PATH}")
        with db.engine.connect() as conn:
            existing = {r[0] for r in conn.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type='table'"))}

        for model in (ArtistImage, EventImage):
            name = model.__tablename__
            if name in existing:
                print(f"Table '{name}' already exists — skipped.")
                continue
            model.__table__.create(db.engine)
            print(f"Created table '{name}'.")


if __name__ == "__main__":
    main()
