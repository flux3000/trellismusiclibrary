"""
migrate_add_remote_favorites.py — Add the remote_favorite table.

A listener browsing someone else's library needs their own favourites, and
`Recording.is_favorite` cannot hold them: the recording lives in a different
database on a different machine.

Stores only (remote_node_id, remote_recording_id) — no metadata about the
recording, so nothing here can go stale against the source. See the module
docstring in app/models/remote_favorite.py for why this lives on the CONSUMER
rather than as a peer_favorite table on the sharer (short version: the peer
door has no write endpoints by design, and a listener's taste is not the
sharer's business).

⚠ Run this on the CONSUMER node — the one doing the browsing. On a two-node dev
rig that means pointing FLUX_DB_PATH at the listener's database:

    FLUX_DB_PATH=$PWD/db/node_matt.db python3 scripts/migrate_add_remote_favorites.py

Additive and idempotent — safe to re-run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
from app.models.remote_favorite import RemoteFavorite
from config import Config
import sqlalchemy as sa


def main():
    app = create_app()
    with app.app_context():
        print(f"database: {Config.DB_PATH}")
        with db.engine.connect() as conn:
            names = [r[0] for r in conn.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type='table'"))]
            if "remote_favorite" in names:
                print("Table 'remote_favorite' already exists — nothing to do.")
                return

        # create_all only creates what is missing, so this adds the one table
        # without touching anything else.
        RemoteFavorite.__table__.create(db.engine)
        print("Created table 'remote_favorite' "
              "(unique on remote_node_id + remote_recording_id).")


if __name__ == "__main__":
    main()
