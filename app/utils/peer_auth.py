"""app/utils/peer_auth.py — the peer authentication door.

Entirely separate from the local `@login_required` (flask_login) path. A peer
never gets a cookie session and never becomes `current_user`; they present a
Bearer token that resolves to a Peer stored on `flask.g`. The peer-facing
blueprint (api/share.py) is the ONLY place `@peer_required` is used, and it
contains no editing endpoints — so a peer token is structurally incapable of
reaching anything that mutates the library.

Secrets (tokens + invite codes) are generated here, shown to the human ONCE,
and stored only as SHA-256 hashes. Verification re-hashes the presented
secret and looks up the hash — the raw value never lives in the DB.
"""

import hashlib
import secrets
from datetime import datetime, timezone
from functools import wraps

from flask import request, jsonify, g

from app.extensions import db
from app.models.peer import Peer, PeerToken


def _utcnow():
    return datetime.now(timezone.utc)


# ── Secret generation + hashing ───────────────────────────────────────────────

def generate_token():
    """A durable per-device peer credential. Opaque, unguessable (~43 chars)."""
    return secrets.token_urlsafe(32)


def generate_invite_code():
    """A one-time enrollment code. Shorter (~12 chars) since a human transfers
    it, but still far beyond brute-forceable given expiry + rate limiting."""
    return secrets.token_urlsafe(9)


def hash_secret(raw):
    """SHA-256 hex of a token or invite code — what actually lives in the DB.
    64 chars, matching the VARCHAR(64) columns."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── Token resolution ──────────────────────────────────────────────────────────

def resolve_peer_token(raw):
    """Resolve a raw bearer token to (PeerToken, Peer), or None if it's
    unknown / revoked / the peer itself is revoked. Touches last_used_at
    (token) and last_seen_at (peer) on success."""
    if not raw:
        return None
    token = db.session.query(PeerToken).filter_by(token_hash=hash_secret(raw)).first()
    if token is None or not token.is_active:
        return None
    peer = token.peer
    if peer is None or not peer.is_active:
        return None
    now = _utcnow()
    token.last_used_at = now
    peer.last_seen_at = now
    db.session.commit()
    return token, peer


def _bearer_from_request():
    """Pull the raw token out of `Authorization: Bearer <token>`."""
    header = request.headers.get("Authorization", "")
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


# ── The decorator ─────────────────────────────────────────────────────────────

def peer_required(f):
    """Gate an endpoint behind a valid, active peer token. On success attaches
    `g.peer` and `g.peer_token`; on failure returns 401 and never reaches the
    view. Parallel to flask_login's @login_required, but a completely separate
    identity path — the two never mix."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        raw = _bearer_from_request()
        if not raw:
            return jsonify({"error": "Missing bearer token"}), 401
        resolved = resolve_peer_token(raw)
        if resolved is None:
            return jsonify({"error": "Invalid or revoked token"}), 401
        token, peer = resolved
        g.peer = peer
        g.peer_token = token
        return f(*args, **kwargs)
    return wrapper


def current_peer():
    """The Peer bound to this request by @peer_required, or None outside a
    peer-authenticated request."""
    return g.get("peer")
