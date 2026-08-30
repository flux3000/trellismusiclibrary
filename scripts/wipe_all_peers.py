"""
scripts/wipe_all_peers.py

Permanently deletes every peer, invite, device token, collection grant and
access-log row -- a clean slate for the sharing feature. Does NOT touch your
library (recordings/tracks/performers/etc), your own account, or Collections
themselves; only the peer-sharing tables.

Goes through the ORM and deletes Peer rows only -- CollectionGrant, PeerInvite,
PeerToken and PeerAccessLog all cascade off Peer (cascade="all, delete-orphan"
in app/models/peer.py), so one delete clears all five tables consistently.

DRY-RUN by default; add --commit to apply.

    python3 scripts/wipe_all_peers.py
    python3 scripts/wipe_all_peers.py --commit
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.extensions import db
from app.models.peer import Peer, CollectionGrant, PeerInvite, PeerToken, PeerAccessLog


def main(commit):
    app = create_app()
    with app.app_context():
        peers = db.session.query(Peer).all()
        print(f"Peers: {len(peers)}")
        for p in peers:
            print(f"  [{p.id}] {p.name!r}  "
                  f"grants={len(p.grants)} invites={len(p.invites)} "
                  f"tokens={len(p.tokens)} accesses={len(p.accesses)}")

        for label, model in [("collection_grant", CollectionGrant),
                              ("peer_invite", PeerInvite),
                              ("peer_token", PeerToken),
                              ("peer_access_log", PeerAccessLog)]:
            n = db.session.query(model).count()
            print(f"  (total {label}: {n})")

        if not peers:
            print("\nAlready empty. Nothing to do.")
            return

        if not commit:
            print(f"\nDRY RUN -- would delete {len(peers)} peer(s) and everything "
                  "cascading off them. Re-run with --commit to apply.")
            return

        for p in peers:
            db.session.delete(p)
        db.session.commit()
        print(f"\nDeleted {len(peers)} peer(s) and all cascaded rows. "
              "Sharing is a clean slate.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true",
                     help="actually delete (default is dry-run/report only)")
    args = ap.parse_args()
    main(args.commit)
