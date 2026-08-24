"""
setup_node_b.py — Build the second node for the peer-sharing dev rig.

Milestone 2 (the consumer side) cannot be developed against nothing: node A
needs a real remote to enroll into, over real HTTP, with a real token. This
script produces that remote.

What it does
------------
1. Copies the live `fluxaudio.db` to a scratch DB (node B's own database)
2. Wipes node B's peer tables — the copy otherwise carries Roy and Trevor
3. Renames node B's collections with a `[NODE B]` suffix
4. Creates one peer ("Node A"), grants it one collection, mints an invite
5. Prints the invite code and the exact command to start node B

Why steps 2 and 3 matter
------------------------
A copied database has IDENTICAL recording ids to the local one. That is exactly
the condition under which a proxy bug — local rows served where remote were
expected — is invisible, because both answers look plausible. Renaming node B's
collections makes provenance obvious on screen: if the UI says `[NODE B]`, the
data really did travel.

The peer wipe is not cosmetic either. Roy and Trevor exist on node A as real
(if unredeemed) peers; leaving copies on node B means two nodes claiming the
same relationships, and a confused mind reading either database.

Run:
    python3 scripts/setup_node_b.py            # build it
    python3 scripts/setup_node_b.py --force    # rebuild, overwriting node B
"""

import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO = Path(__file__).parent.parent
SOURCE_DB = REPO / "db" / "fluxaudio.db"
NODE_B_DB = REPO / "db" / "node_b.db"          # *.db is gitignored
NODE_B_PORT = 5758                              # node A is 5757
NODE_B_NAME = "Node B — Test Library"
NODE_B_OWNER = "Test Owner"


def build_database(force):
    if not SOURCE_DB.exists():
        sys.exit(f"Source database not found: {SOURCE_DB}")

    if NODE_B_DB.exists():
        if not force:
            sys.exit(
                f"{NODE_B_DB} already exists.\n"
                f"Re-run with --force to rebuild it (this discards node B's "
                f"peers, tokens and invites)."
            )
        NODE_B_DB.unlink()

    shutil.copy2(SOURCE_DB, NODE_B_DB)
    # SQLite WAL siblings, if present, would carry stale pages into the copy.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(SOURCE_DB) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(NODE_B_DB) + suffix))
    print(f"✓ Copied database → {NODE_B_DB}")


def seed(force):
    build_database(force)

    # create_app() reads FLUX_DB_PATH, so point it at node B BEFORE importing
    # anything that builds the engine.
    os.environ["FLUX_DB_PATH"] = str(NODE_B_DB)
    os.environ["SHARE_NODE_NAME"] = NODE_B_NAME
    os.environ["SHARE_OWNER_NAME"] = NODE_B_OWNER
    os.environ["SHARE_BASE_URL"] = f"http://127.0.0.1:{NODE_B_PORT}"

    from app import create_app
    from app.extensions import db
    from app.models.collection import Collection
    from app.models.peer import Peer, CollectionGrant, PeerInvite, PeerToken, PeerAccessLog
    from app.models.remote_node import RemoteNode
    from app.utils.peer_auth import generate_invite_code, hash_secret
    from config import Config

    app = create_app()
    with app.app_context():
        # Guard against ever pointing this at the live database.
        assert str(Config.DB_PATH) == str(NODE_B_DB), (
            f"Refusing to seed: config resolved to {Config.DB_PATH}, "
            f"not {NODE_B_DB}"
        )

        # 1. Wipe inherited peer state — children before parents.
        for model in (PeerAccessLog, PeerToken, PeerInvite, CollectionGrant, Peer):
            deleted = db.session.query(model).delete()
            if deleted:
                print(f"  cleared {deleted:>4} × {model.__name__}")
        db.session.commit()

        # 1b. Wipe INHERITED remote_node rows (the outbound half). The copy
        # carries node A's joined libraries, one of which points at node B —
        # so without this, node B boots believing it has joined ITSELF, and its
        # library selector offers a bogus entry beside the real one.
        #
        # ⚠ Rows only — the keychain is deliberately NOT touched. Tokens are
        # stored as `remote_token:<node_id>` with no namespacing by node, so on
        # a single dev machine node A and node B SHARE those entries. Deleting
        # `remote_token:1` here would silently break node A's own enrollment.
        # Orphaned keychain items are harmless; a broken node A is not.
        stale = db.session.query(RemoteNode).delete()
        if stale:
            print(f"  cleared {stale:>4} × RemoteNode (inherited; keychain left alone)")
        db.session.commit()

        # 2. Mark node B's collections so their provenance is visible on screen.
        collections = db.session.query(Collection).order_by(Collection.id).all()
        if not collections:
            sys.exit("Node B has no collections — nothing to grant. Aborting.")
        for c in collections:
            if not c.name.endswith("[NODE B]"):
                c.name = f"{c.name} [NODE B]"
        db.session.commit()
        print(f"✓ Renamed {len(collections)} collections with [NODE B]")

        # 3. One peer, one grant, one invite — the minimum to enroll node A.
        peer = Peer(name="Node A", contact_note="The dev rig's consumer node")
        db.session.add(peer)
        db.session.flush()

        # Grant BOTH a system collection (if this DB has one) and a curated
        # one, because UC1 needs both behaviours proved in a single run:
        #   - Full Library must resolve to every published recording, so the
        #     peer's library is the whole shelf rather than a handful of rows;
        #   - the system collection must NOT appear in the peer's Collections
        #     list, while the curated one must — a peer already browsing the
        #     whole library does not also need a "collection" containing it.
        # Granting only one of the two leaves half of that untested.
        system = [c for c in collections if c.system_key]
        curated = [c for c in collections if not c.system_key]
        targets = system[:1] + curated[:1]
        for t in targets:
            db.session.add(CollectionGrant(peer_id=peer.id, collection_id=t.id))

        # expires_at is nullable=False with no model default — mint_invite sets
        # it, and a script bypassing that endpoint has to set it too. 90 days
        # (the endpoint's own ceiling) so the rig doesn't expire mid-build.
        raw_code = generate_invite_code()
        db.session.add(PeerInvite(
            peer_id=peer.id,
            code_hash=hash_secret(raw_code),
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        ))
        db.session.commit()

        # .recording_count, not len(.recordings): the latter materialises every
        # row, which for Full Library means building 580 ORM objects to print
        # one number.
        for t in targets:
            kind = "system" if t.system_key else "curated"
            print(f"✓ Peer 'Node A' granted “{t.name}” "
                  f"({t.recording_count} recordings, {kind})")
        if not system:
            print("  ! No system collection in this database — run "
                  "scripts/migrate_add_system_collections.py first if you "
                  "meant to test Full Library sharing.")

    print()
    print("─" * 68)
    print("  INVITE CODE (shown once — node B stores only its hash)")
    print()
    print(f"      {raw_code}")
    print()
    print(f"  Full invite string:  http://127.0.0.1:{NODE_B_PORT}#{raw_code}")
    print("─" * 68)
    print()
    print("Start node B with:")
    print()
    print(f"    cd {REPO} && \\")
    print(f"    FLUX_DB_PATH={NODE_B_DB} \\")
    print(f"    FLUX_PORT={NODE_B_PORT} \\")
    print(f"    SECRET_KEY=node-b-not-node-a \\")
    print(f"    FLUX_COOKIE_NAME=session_node_b \\")
    print(f'    SHARE_BASE_URL=http://127.0.0.1:{NODE_B_PORT} \\')
    print(f'    SHARE_NODE_NAME="{NODE_B_NAME}" \\')
    print(f'    SHARE_OWNER_NAME="{NODE_B_OWNER}" \\')
    print(f"    python3 run_headless.py")
    print()
    print("Node A stays as it is: `DEV_MODE=true python3 run.py` on 5757.")
    print()
    print("SECRET_KEY and FLUX_COOKIE_NAME are NOT optional. Browser cookies")
    print("are scoped by host and ignore the port, so without both, node A's")
    print("admin session authenticates you against node B — and each node's")
    print("cookie silently overwrites the other's.")
    print()
    print("NOTE: node B serves audio from the SAME LIBRARY_ROOT as node A —")
    print("both point at /Volumes/music. That is fine for the rig (streaming")
    print("really does work) but means node B is not a truly independent")
    print("library. Do not read 'the file was found' as proof of the proxy.")


if __name__ == "__main__":
    seed(force="--force" in sys.argv)
