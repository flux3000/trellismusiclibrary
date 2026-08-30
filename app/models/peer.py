"""
models/peer.py — Peer sharing (inbound side).

The five tables that let this instance share Collections OUT to named remote
people. See "Peer Sharing — Design Spec v1" in the Drive Context Library for
the full rationale. Short version:

  Peer            — a named remote person I share with (Matt). NOT a `user`;
                    a peer authenticates through a totally separate door
                    (Bearer token, api/share.py) with no route to any editing
                    endpoint. Identity is per-node: I'm an admin `user` in my
                    own DB and a `Peer` in everyone else's, never both here.
  CollectionGrant — peer <-> collection. A peer sees every recording in every
                    collection they hold a live grant to. Permanent until
                    revoked (soft revoke via revoked_at).
  PeerInvite      — one-time, expiring enrollment code (hashed at rest). The
                    peer is created first, THEN invited; the invite only mints
                    a token, it doesn't create the peer.
  PeerToken       — the durable credential the peer's app holds after joining
                    (hashed at rest). Multiple allowed per peer (multi-device),
                    each individually revocable.
  PeerAccessLog   — one row per peer stream (who streamed what, when). Feeds
                    last_seen_at and the peer-management activity read.

The OUTBOUND side (libraries I consume from others) is a separate concern —
see models/remote_node.py (built in the client-side milestone), not here.
"""

from datetime import datetime, timezone
from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class Peer(db.Model):
    """A named remote person this instance shares Collections out to."""
    __tablename__ = "peer"

    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(255), nullable=False)   # my local label for them
    contact_note = db.Column(db.Text,        nullable=True)    # "met at etree, bluegrass guy"
    created_at   = db.Column(db.DateTime, default=_utcnow)
    last_seen_at = db.Column(db.DateTime, nullable=True)       # touched on any authed request
    revoked_at   = db.Column(db.DateTime, nullable=True)       # nuclear option — kills the peer everywhere

    grants   = db.relationship("CollectionGrant", back_populates="peer",
                               cascade="all, delete-orphan")
    invites  = db.relationship("PeerInvite", back_populates="peer",
                               cascade="all, delete-orphan")
    tokens   = db.relationship("PeerToken", back_populates="peer",
                               cascade="all, delete-orphan")
    accesses = db.relationship("PeerAccessLog", back_populates="peer",
                               cascade="all, delete-orphan")

    @property
    def is_active(self):
        return self.revoked_at is None

    @property
    def active_grants(self):
        return [g for g in self.grants if g.is_active]

    def __repr__(self):
        state = "active" if self.is_active else "revoked"
        return f"<Peer {self.name} ({state})>"


class CollectionGrant(db.Model):
    """peer <-> collection. Permanent until revoked (soft revoke)."""
    __tablename__ = "collection_grant"

    id            = db.Column(db.Integer, primary_key=True)
    peer_id       = db.Column(db.Integer, db.ForeignKey("peer.id"),       nullable=False, index=True)
    collection_id = db.Column(db.Integer, db.ForeignKey("collection.id"), nullable=False, index=True)
    created_at    = db.Column(db.DateTime, default=_utcnow)
    revoked_at    = db.Column(db.DateTime, nullable=True)

    peer       = db.relationship("Peer", back_populates="grants")
    collection = db.relationship("Collection")

    # NOTE: deliberately NO unique(peer_id, collection_id) — soft revoke means a
    # peer can hold a revoked grant AND a fresh active grant to the same
    # collection over time. "One active grant per (peer, collection)" is
    # enforced in app logic when granting, not by the schema.

    @property
    def is_active(self):
        return self.revoked_at is None and (self.peer is None or self.peer.is_active)

    def __repr__(self):
        return f"<CollectionGrant peer={self.peer_id} collection={self.collection_id}>"


class PeerInvite(db.Model):
    """Reusable, expiring enrollment code. Raw code shown to the sharer once;
    only its SHA-256 hash is stored.

    Reusable as of 2026-08-30 (Ryan): the code is no longer burned on first
    join, so the same link can enroll more than one device without minting a
    fresh one each time. `consumed_at` is kept -- it still records WHEN it
    was first used (drives the "Used · joined <date>" admin display), it just
    no longer gates validity. Only expiry does that now."""
    __tablename__ = "peer_invite"

    id          = db.Column(db.Integer, primary_key=True)
    peer_id     = db.Column(db.Integer, db.ForeignKey("peer.id"), nullable=False, index=True)
    code_hash   = db.Column(db.String(64), unique=True, nullable=False, index=True)  # sha256 hex
    created_at  = db.Column(db.DateTime, default=_utcnow)
    expires_at  = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=True)    # first-use timestamp, display only

    peer = db.relationship("Peer", back_populates="invites")

    def is_valid(self, now=None):
        now = now or _utcnow()
        exp = self.expires_at
        # tolerate naive timestamps coming back from SQLite (stored UTC)
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp is None or now < exp

    def __repr__(self):
        return f"<PeerInvite peer={self.peer_id} consumed={self.consumed_at is not None}>"


class PeerToken(db.Model):
    """Durable per-device credential a peer's app holds. Hashed at rest."""
    __tablename__ = "peer_token"

    id           = db.Column(db.Integer, primary_key=True)
    peer_id      = db.Column(db.Integer, db.ForeignKey("peer.id"), nullable=False, index=True)
    token_hash   = db.Column(db.String(64), unique=True, nullable=False, index=True)  # sha256 hex
    device_label = db.Column(db.String(255), nullable=True)   # "Matt's MacBook" — multi-device later
    created_at   = db.Column(db.DateTime, default=_utcnow)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at   = db.Column(db.DateTime, nullable=True)      # kill one device without killing the peer

    peer = db.relationship("Peer", back_populates="tokens")

    @property
    def is_active(self):
        return self.revoked_at is None and (self.peer is None or self.peer.is_active)

    def __repr__(self):
        return f"<PeerToken peer={self.peer_id} device={self.device_label!r}>"


class PeerAccessLog(db.Model):
    """One row per peer stream start. Minimal by design — drives last_seen_at
    and the peer activity read, and seeds the future change-log ethos."""
    __tablename__ = "peer_access_log"

    id          = db.Column(db.Integer, primary_key=True)
    peer_id     = db.Column(db.Integer, db.ForeignKey("peer.id"),  nullable=False, index=True)
    track_id    = db.Column(db.Integer, db.ForeignKey("track.id"), nullable=False, index=True)
    occurred_at = db.Column(db.DateTime, default=_utcnow, index=True)

    peer  = db.relationship("Peer", back_populates="accesses")
    track = db.relationship("Track")

    def __repr__(self):
        return f"<PeerAccessLog peer={self.peer_id} track={self.track_id} at={self.occurred_at}>"
