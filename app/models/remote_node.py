"""models/remote_node.py — OUTBOUND sharing: libraries I have joined as a peer.

The mirror image of models/peer.py. `peer` records who may consume MY library;
`remote_node` records whose libraries I consume. Every install carries both
tables — which side is "active" depends only on which direction data happens
to be flowing.

WHY THERE IS NO TOKEN COLUMN
----------------------------
The original sketch put `my_token` on this table.

It cannot be hashed — unlike peer_token on the inbound side, this credential
has to be replayed verbatim on every proxied request, so a one-way hash is not
an option. That leaves plaintext-in-DB or the keychain, and a database that is
copied to a dev rig, backed up, and synced is a poor home for a live credential
to someone else's library.

See app/utils/prefs.py: get_remote_token / set_remote_token / delete_remote_token.
"""

from datetime import datetime, timezone

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class RemoteNode(db.Model):
    """A Trellis library I have enrolled into, and may browse and stream from."""
    __tablename__ = "remote_node"

    id           = db.Column(db.Integer, primary_key=True)

    # How this library shows up in the library selector. Seeded from the
    # remote's own SHARE_NODE_NAME at enroll, then editable locally — it is my
    # label for their library, the same way peer.name is my label for a person.
    display_name = db.Column(db.String(255), nullable=False)

    # Their address, e.g. https://matt-flux.example.com or http://127.0.0.1:5758.
    # Stored without a trailing slash (normalised on enroll) so proxy path
    # joining never produces a double slash.
    base_url     = db.Column(db.String(512), nullable=False)

    # Who they said they were at enroll. Informational — shown in the UI so a
    # library can be identified by its owner, not just its name.
    owner_name   = db.Column(db.String(255), nullable=True)

    # What THEY call me. Useful when a peer holds several relationships, and
    # the only place my identity on their node is recorded.
    peer_name    = db.Column(db.String(255), nullable=True)

    enrolled_at        = db.Column(db.DateTime, default=_utcnow)
    last_connected_at  = db.Column(db.DateTime, nullable=True)

    # Set when we leave a remote. Kept rather than deleted for the same reason
    # revoked peers stay visible: "I used to have access to this" is
    # information, and a vanished row is indistinguishable from one that was
    # never there.
    left_at      = db.Column(db.DateTime, nullable=True)

    @property
    def is_active(self):
        return self.left_at is None

    def __repr__(self):
        return f"<RemoteNode {self.display_name!r} at {self.base_url}>"
