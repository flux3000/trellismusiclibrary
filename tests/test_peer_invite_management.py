"""
tests/test_peer_invite_management.py — cancelling an unused invite is NOT
revoking a peer, and the two must never be confusable.

Added 2026-08-25. Until now the Peers page offered exactly one destructive
control — "Revoke access" — and old unused invites could neither be seen nor
cancelled. Those are different blast radii:

    revoke peer      → every device dies, their library goes dark
    cancel invite    → one unused code stops working, nobody in-flight is hit

A USED invite is deliberately NOT deletable. The token it minted still works,
so deleting the row would erase the record of an access that is still live —
killing THAT is a device revocation, a third action again.
"""

from datetime import datetime, timezone, timedelta

import pytest

from app.extensions import db as _db
from app.models.user import User
from app.models.peer import Peer, PeerInvite, PeerToken
from app.utils.peer_auth import generate_invite_code, hash_secret

from tests.test_peer_sharing import _login_as


def _utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture()
def peer(app):
    p = Peer(name="Jim Bricker")
    _db.session.add(p)
    _db.session.commit()
    return p


def _invite(peer, *, days=7, consumed=False):
    inv = PeerInvite(
        peer_id=peer.id,
        code_hash=hash_secret(generate_invite_code()),
        expires_at=_utcnow() + timedelta(days=days),
        consumed_at=_utcnow() if consumed else None,
    )
    _db.session.add(inv)
    _db.session.commit()
    return inv


def _admin(app):
    c = app.test_client()
    _login_as(c)
    return c


# ── The three states are reported distinctly ─────────────────────────────────

def test_detail_reports_pending_used_and_expired(app, peer):
    _invite(peer)                          # pending
    _invite(peer, consumed=True)           # used
    _invite(peer, days=-1)                 # expired

    body = _admin(app).get(f"/api/peers/{peer.id}").get_json()
    assert sorted(i["status"] for i in body["invites"]) == ["expired", "pending", "used"]
    # Every row must be targetable, or the UI can list them and do nothing.
    assert all(isinstance(i["id"], int) for i in body["invites"])


# ── Cancelling ───────────────────────────────────────────────────────────────

def test_an_unused_invite_can_be_cancelled(app, peer):
    inv = _invite(peer)
    r = _admin(app).delete(f"/api/peers/{peer.id}/invites/{inv.id}")
    assert r.status_code == 200 and r.get_json()["was"] == "pending"
    assert _db.session.get(PeerInvite, inv.id) is None


def test_an_expired_invite_can_be_cleared(app, peer):
    inv = _invite(peer, days=-1)
    r = _admin(app).delete(f"/api/peers/{peer.id}/invites/{inv.id}")
    assert r.status_code == 200 and r.get_json()["was"] == "expired"


def test_a_used_invite_is_refused_and_survives(app, peer):
    inv = _invite(peer, consumed=True)
    r = _admin(app).delete(f"/api/peers/{peer.id}/invites/{inv.id}")
    assert r.status_code == 409
    assert r.get_json()["code"] == "invite_consumed"
    assert _db.session.get(PeerInvite, inv.id) is not None, "history must survive"


# ── Blast radius: this is not revoke ─────────────────────────────────────────

def test_cancelling_does_not_touch_the_peer_or_their_devices(app, peer):
    """The whole reason this control exists. Someone already listening must
    not be disconnected by tidying up an old code."""
    live = PeerToken(peer_id=peer.id, token_hash=hash_secret("tok"))
    _db.session.add(live)
    inv = _invite(peer)
    _db.session.commit()

    assert _admin(app).delete(
        f"/api/peers/{peer.id}/invites/{inv.id}").status_code == 200

    _db.session.refresh(peer)
    assert peer.revoked_at is None,        "the peer must not be revoked"
    assert _db.session.get(PeerToken, live.id).revoked_at is None, \
        "an existing device must keep working"


# ── Addressing ───────────────────────────────────────────────────────────────

def test_an_invite_cannot_be_deleted_through_the_wrong_peer(app, peer):
    """peer_id is part of the address, not decoration — otherwise guessing an
    integer deletes someone else's invite."""
    other = Peer(name="Someone Else")
    _db.session.add(other)
    _db.session.commit()
    inv = _invite(peer)

    r = _admin(app).delete(f"/api/peers/{other.id}/invites/{inv.id}")
    assert r.status_code == 404
    assert _db.session.get(PeerInvite, inv.id) is not None


def test_unknown_invite_is_404(app, peer):
    assert _admin(app).delete(
        f"/api/peers/{peer.id}/invites/999999").status_code == 404


# ── The door is still admin-only ─────────────────────────────────────────────

def test_deleting_an_invite_requires_an_admin_session(app, peer):
    inv = _invite(peer)
    r = app.test_client().delete(f"/api/peers/{peer.id}/invites/{inv.id}")
    assert r.status_code in (401, 403), \
        "an unauthenticated caller must not reach this at all"
    assert _db.session.get(PeerInvite, inv.id) is not None
