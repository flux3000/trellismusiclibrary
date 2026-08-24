"""
setup_consumer_node.py — Build an EMPTY second node: a listener's machine.

WHY THIS EXISTS (2026-08-24)
----------------------------
`setup_node_b.py` builds the other kind of second node: a COPY of this library,
which is right for testing Archivist↔Archivist trading (UC3), where both sides
genuinely own collections.

It is wrong for the Streamer (UC1). Matt does not collect. He owns nothing,
runs no archive, and wants only to listen to what someone shared with him. A
node seeded from a clone of Ryan's database gives "Matt" 580 local recordings,
which:

  1. contradicts the persona outright;
  2. makes remote and local data indistinguishable on screen — the reason
     setup_node_b.py has to rename every collection "[NODE B]" is precisely
     this, and a suffix is a workaround for a rig that should not have had the
     problem;
  3. hides the experience we actually need to see, which is an EMPTY library
     that fills up the moment a remote is joined. If everything on screen came
     from the remote, then anything on screen proves the proxy works.

So this builds a node with schema, one user, and nothing else.

Run:
    python3 scripts/setup_consumer_node.py                 # build Matt's node
    python3 scripts/setup_consumer_node.py --force         # rebuild it
    python3 scripts/setup_consumer_node.py --name Dana --port 5759
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO = Path(__file__).parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="Matt", help="the listener's name")
    ap.add_argument("--port", type=int, default=5758)
    ap.add_argument("--db", default=None, help="path to this node's database")
    ap.add_argument("--force", action="store_true", help="rebuild if it exists")
    args = ap.parse_args()

    slug = args.name.strip().lower().replace(" ", "_")
    db_path = Path(args.db) if args.db else REPO / "db" / f"node_{slug}.db"

    if db_path.exists():
        if not args.force:
            sys.exit(f"{db_path} already exists. Re-run with --force to rebuild it.")
        db_path.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()

    node_name = f"{args.name}'s Library"

    # create_app() resolves DB_PATH from config at import time, so the
    # environment has to be set BEFORE anything builds the engine.
    os.environ["FLUX_DB_PATH"] = str(db_path)
    os.environ["SHARE_NODE_NAME"] = node_name
    os.environ["SHARE_OWNER_NAME"] = args.name
    os.environ["SHARE_BASE_URL"] = f"http://127.0.0.1:{args.port}"

    import bcrypt
    from app import create_app
    from app.extensions import db
    import app.models                 # noqa: F401 — importing the package
                                      # registers every model with SQLAlchemy,
                                      # which db.create_all() needs. A star
                                      # import would be illegal inside a
                                      # function.
    from app.models.user import User
    from app.models.recording import Recording
    from config import Config

    app = create_app()
    with app.app_context():
        assert str(Config.DB_PATH) == str(db_path), (
            f"Refusing to build: config resolved to {Config.DB_PATH}, not {db_path}")

        db.create_all()
        print(f"✓ Empty schema created at {db_path}")

        username = slug
        password = slug          # DEV_MODE auto-logs-in anyway; this is a rig
        admin = User(
            username      = username,
            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
            role          = "admin",
            all_artists   = True,
            is_active     = True,
        )
        db.session.add(admin)
        db.session.commit()
        print(f"✓ User '{username}' created (password: {password})")

        # Prove the point of this script rather than assuming it.
        n = db.session.query(Recording).count()
        assert n == 0, f"expected an empty library, found {n} recordings"
        print(f"✓ Library is empty ({n} recordings) — everything {args.name} "
              f"sees will have come from a remote.")

    print()
    print("─" * 68)
    print(f"  Start {args.name}'s node with:")
    print()
    print(f"    cd {REPO} && \\")
    print(f"    DEV_MODE=true \\")
    print(f"    FLUX_DB_PATH={db_path} \\")
    print(f"    FLUX_PORT={args.port} \\")
    print(f"    SECRET_KEY={slug}-node-secret \\")
    print(f"    FLUX_COOKIE_NAME=session_{slug} \\")
    print(f'    SHARE_BASE_URL=http://127.0.0.1:{args.port} \\')
    print(f'    SHARE_NODE_NAME="{node_name}" \\')
    print(f'    SHARE_OWNER_NAME="{args.name}" \\')
    print(f"    python3 run_headless.py")
    print()
    print(f"  Then open http://127.0.0.1:{args.port} — that is {args.name}'s")
    print(f"  machine. It will be empty until a library is joined.")
    print("─" * 68)
    print()
    print("SECRET_KEY and FLUX_COOKIE_NAME are NOT optional: browser cookies are")
    print("scoped by host and ignore the port, so without both, the two nodes")
    print("authenticate as each other and overwrite each other's sessions.")


if __name__ == "__main__":
    main()
