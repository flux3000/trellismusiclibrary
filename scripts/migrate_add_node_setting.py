"""
migrate_add_node_setting.py — Add the node_setting table.

node_setting holds install-level key/value facts (currently just
share_base_url — the public address peers enroll through) that live with
the node itself, not with any one user account. See app/models/node_setting.py
and app/utils/node_settings.py.

create_all() only fires on a genuinely fresh install (see first_run_setup()
in run.py) — an existing checkout is left alone on every later boot, on
purpose, so a half-finished model can never silently conjure a table and
hide a skipped migration. That means every new table needs one of these.

Without this, any code path touching node_setting (Settings > Sharing >
Public address, and minting an invite) hits "no such table: node_setting"
on an existing database, which Flask returns as an HTML error page instead
of JSON — and Safari/WKWebView reports that as the unhelpful
"The string did not match the expected pattern." (2026-08-30, Ryan hit
this on both the Public address save and Create invite.)

Additive and idempotent — safe to re-run.

    python3 scripts/migrate_add_node_setting.py
    FLUX_DB_PATH=/path/to/other.db python3 scripts/migrate_add_node_setting.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
from app.models.node_setting import NodeSetting
from config import Config
import sqlalchemy as sa


def main():
    app = create_app()
    with app.app_context():
        print(f"database: {Config.DB_PATH}")
        with db.engine.connect() as conn:
            names = [r[0] for r in conn.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type='table'"))]
            if "node_setting" in names:
                print("Table 'node_setting' already exists — nothing to do.")
                return

        # create_all only creates what is missing, so this adds the one
        # table without touching anything else.
        NodeSetting.__table__.create(db.engine)
        print("Created table 'node_setting'. Public address / SHARE_BASE_URL "
              "can now be set from Settings > Sharing.")


if __name__ == "__main__":
    main()
