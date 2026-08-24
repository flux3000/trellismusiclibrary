"""
reset_peer_state.py — Hard-delete this node's peer-sharing state for a fresh start.

WHY THIS EXISTS
---------------
`POST /api/peers/<id>/revoke` is a SOFT revoke: it stamps `revoked_at` and
leaves the row, which is correct for a real relationship you may want a record
of. It is wrong for test fixtures. Roy and Trevor were created on 2026-08-09 to
prove the admin UI worked, their invites were never usable, and carrying them
forward as revoked rows means every future query has to explain them.

There is no DELETE endpoint for a peer, deliberately — deleting a real peer
throws away the access log, which is the one record of what they streamed. This
script is the escape hatch, kept out of the API on purpose.

WHAT IT DELETES
---------------
INBOUND (people who consume MY library):
  Peer, and by cascade CollectionGrant, PeerInvite, PeerToken, PeerAccessLog.

OUTBOUND (libraries I consume):
  RemoteNode rows, plus each one's OS-keychain token.

The outbound half matters more than it looks. A `remote_node` row whose remote
has been rebuilt still shows in the library selector and still sends a token the
remote no longer recognises — every proxied call 401s and the UI reports
"failed to load library". Worse, `POST /api/remotes/enroll` refuses with 409
"Already joined" while such a row exists, so re-enrolling with a fresh invite
bounces with an error that looks like a bad invite. Leaving the stale row is
therefore not a cosmetic problem; it blocks recovery.

⚠ Respects FLUX_DB_PATH, so it can be pointed at node B. It prints which
database it resolved BEFORE doing anything — read that line.

Dry run by default. Nothing is written without --commit.

    python3 scripts/reset_peer_state.py                    # show what would go
    python3 scripts/reset_peer_state.py --commit           # do it
    python3 scripts/reset_peer_state.py --commit --inbound-only
    python3 scripts/reset_peer_state.py --commit --outbound-only
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
from app.models.peer import Peer, CollectionGrant, PeerInvite, PeerToken, PeerAccessLog
from app.models.remote_node import RemoteNode
from app.utils.prefs import delete_remote_token, get_remote_token
from config import Config


def main():
    commit = "--commit" in sys.argv
    inbound = "--outbound-only" not in sys.argv
    outbound = "--inbound-only" not in sys.argv

    app = create_app()
    with app.app_context():
        print("─" * 68)
        print(f"  database : {Config.DB_PATH}")
        print(f"  mode     : {'COMMIT — this will delete rows' if commit else 'DRY RUN — nothing will be written'}")
        print("─" * 68)

        if inbound:
            peers = db.session.query(Peer).order_by(Peer.id).all()
            print(f"\nINBOUND — peers who consume this library ({len(peers)}):")
            if not peers:
                print("  (none)")
            for p in peers:
                # Counted explicitly rather than trusting the cascade silently:
                # if these numbers look wrong, stop before --commit.
                g = db.session.query(CollectionGrant).filter_by(peer_id=p.id).count()
                i = db.session.query(PeerInvite).filter_by(peer_id=p.id).count()
                t = db.session.query(PeerToken).filter_by(peer_id=p.id).count()
                a = db.session.query(PeerAccessLog).filter_by(peer_id=p.id).count()
                state = "active" if p.is_active else "revoked"
                print(f"  [{p.id}] {p.name} ({state}) — "
                      f"{g} grants, {i} invites, {t} tokens, {a} access-log rows")
            if commit:
                for p in peers:
                    db.session.delete(p)     # cascade handles the four children
                db.session.commit()
                print(f"  → deleted {len(peers)} peers and all their children.")

        if outbound:
            nodes = db.session.query(RemoteNode).order_by(RemoteNode.id).all()
            print(f"\nOUTBOUND — libraries this node has joined ({len(nodes)}):")
            if not nodes:
                print("  (none)")
            for n in nodes:
                has_tok = get_remote_token(n.id) is not None
                left = "left" if n.left_at else "JOINED"
                print(f"  [{n.id}] {n.display_name} @ {n.base_url} — {left}, "
                      f"keychain token: {'present' if has_tok else 'missing'}")
            if commit:
                for n in nodes:
                    # Keychain first: if the row goes and this fails, the token
                    # is orphaned under a node id that no longer exists and
                    # nothing will ever clean it up.
                    try:
                        delete_remote_token(n.id)
                    except Exception as e:
                        print(f"  ! keychain delete failed for node {n.id}: {e}")
                    db.session.delete(n)
                db.session.commit()
                print(f"  → deleted {len(nodes)} remote nodes and their keychain tokens.")

        print()
        if not commit:
            print("Dry run only. Re-run with --commit to apply.")
        else:
            print("Done. Re-enroll with a fresh invite when ready.")


if __name__ == "__main__":
    main()
