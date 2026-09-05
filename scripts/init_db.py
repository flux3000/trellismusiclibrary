"""
scripts/init_db.py — Initialize the database and seed an admin user.

Run once after setting up the project:
    python scripts/init_db.py

Creates db/trellis.db with all tables, then prompts for admin credentials.
Safe to re-run — will not overwrite an existing admin user.
"""

import sys
import os

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcrypt
from app import create_app
from app.extensions import db
from app.models import *   # registers all models with SQLAlchemy


def init_db():
    app = create_app()
    with app.app_context():
        # Ensure db directory exists
        os.makedirs(app.config["DB_PATH"].parent, exist_ok=True)

        # Create all tables
        db.create_all()
        print("✓ Schema created.")

        # Seed admin user if none exists
        from app.models.user import User
        if not db.session.query(User).filter_by(role="admin").first():
            print("\nNo admin user found. Creating one now.")
            username = input("Admin username: ").strip()
            password = input("Admin password: ").strip()

            if not username or not password:
                print("Username and password are required. Exiting.")
                sys.exit(1)

            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            admin  = User(
                username      = username,
                password_hash = hashed,
                role          = "admin",
                all_artists   = True,
                is_active     = True,
            )
            db.session.add(admin)
            db.session.commit()
            print(f"✓ Admin user '{username}' created.")
        else:
            print("✓ Admin user already exists — skipping seed.")

        print("\nDatabase ready at:", app.config["DB_PATH"])


if __name__ == "__main__":
    init_db()
