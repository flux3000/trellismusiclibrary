"""
scripts/migrate_add_user_profile.py — display_name + avatar_ext on user.

Additive and idempotent; safe to run twice. Adds:

    user.display_name   VARCHAR(120) NULL   what a human sees
    user.avatar_ext     VARCHAR(8)   NULL   '.jpg' etc; file lives in AVATAR_DIR

Both nullable on purpose. Every existing row predates them, and `User.name`
falls back to `username`, so a database that has not run this yet still shows a
name — it just cannot change one.

Run once per node (2026-08-25):
    source .venv/bin/activate && python3 scripts/migrate_add_user_profile.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text                      # noqa: E402
from app import create_app                       # noqa: E402
from app.extensions import db                    # noqa: E402
from config import Config                        # noqa: E402

COLUMNS = [("display_name", "VARCHAR(120)"), ("avatar_ext", "VARCHAR(8)")]


def main():
    app = create_app()
    with app.app_context():
        print(f"database: {Config.DB_PATH}")
        existing = {r[1] for r in db.session.execute(text("PRAGMA table_info(user)"))}
        if not existing:
            sys.exit("No `user` table here — is this the right database?")

        added = 0
        for name, decl in COLUMNS:
            if name in existing:
                print(f"  · user.{name} already present")
                continue
            db.session.execute(text(f"ALTER TABLE user ADD COLUMN {name} {decl}"))
            print(f"  ✓ added user.{name}")
            added += 1
        db.session.commit()

        # Prove it rather than trusting the ALTER.
        now = {r[1] for r in db.session.execute(text("PRAGMA table_info(user)"))}
        missing = [n for n, _ in COLUMNS if n not in now]
        if missing:
            sys.exit(f"FAILED — still missing: {', '.join(missing)}")
        print(f"done ({added} added, {len(COLUMNS) - added} already there)")


if __name__ == "__main__":
    main()
