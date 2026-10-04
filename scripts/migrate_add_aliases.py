"""
migrate_add_aliases.py -- add the venue_alias and artist_alias tables.

Resolver v2, chunk 4 (2026-10-03). Learned aliases: text a person's confirmed
correction taught the resolver ("Wilkes Commnity College" is Merlefest's venue).
The tables ship now and stay empty until chunk 8 fills them; see
app/models/alias.py.

Run it against the database the app uses (from the repo, .venv active):

    python3 scripts/migrate_add_aliases.py

Additive and idempotent: it creates only what is missing, then prints each table's
columns so the result can be checked, and exits non-zero if a column is absent.
A fresh install gets the same tables from create_all().
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlalchemy as sa

from app import create_app
from app.extensions import db
from app.models.alias import ArtistAlias, VenueAlias
from config import Config

EXPECTED = {
    "venue_alias":  {"id", "venue_id", "alias", "alias_key", "created_at"},
    "artist_alias": {"id", "artist_id", "alias", "alias_key", "created_at"},
}


def main():
    app = create_app()
    with app.app_context():
        print(f"database: {Config.DB_PATH}")
        with db.engine.connect() as conn:
            have = {r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
        for model in (VenueAlias, ArtistAlias):
            name = model.__table__.name
            if name in have:
                print(f"Table '{name}' already exists.")
            else:
                model.__table__.create(db.engine)
                print(f"Created table '{name}'.")
        bad = 0
        with db.engine.connect() as conn:
            for name, want in EXPECTED.items():
                cols = {r[1] for r in conn.execute(sa.text(f"PRAGMA table_info({name})"))}
                missing = want - cols
                print(f"  {name}: {', '.join(sorted(cols))}")
                if missing:
                    bad += 1
                    print(f"  !! {name} is missing: {', '.join(sorted(missing))}")
        return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
