"""
tests/test_peer_sharing.py — inbound peer-sharing server side (milestone 1).

Covers the security-critical surface:
  * token hashing + resolution (valid / unknown / revoked token / revoked peer)
  * invite validity (fresh / expired / consumed)
  * collection-scoped access checks (the one gate browse + stream share)
  * the enrollment handshake (invite -> working token, single-use)
  * peer-facing browse scoping (granted 200, ungranted 403, no-token 401)
  * stream authorization (ungranted 403; granted reaches the transcoder and
    404s on the missing seed FLAC — no ffmpeg/real audio needed to prove auth)
  * the structural guarantee: a peer Bearer token buys NOTHING on the local
    editing door (@login_required), and admin peer-mgmt needs an admin session
"""

from datetime import datetime, timezone, timedelta

import pytest

from app.extensions import db as _db
from app.models.user import User
from app.models.peer import Peer, CollectionGrant, PeerInvite, PeerToken
from app.models.collection import Collection, CollectionRecording
from app.models.recording import Recording
from app.models.track import Track

from app.utils.peer_auth import (
    generate_token, generate_invite_code, hash_secret, resolve_peer_token,
)
from app.utils.peer_access import (
    peer_can_access_recording, peer_can_access_track, peer_granted_collection_ids,
)


def _utcnow():
    return datetime.now(timezone.utc)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_collection_with_rec(name="Shared Box"):
    rec = _db.session.query(Recording).first()
    col = Collection(name=name)
    _db.session.add(col)
    _db.session.flush()
    _db.session.add(CollectionRecording(collection_id=col.id, recording_id=rec.id, order=0))
    _db.session.commit()
    return col, rec


def _make_peer(name="Matt", granted_collection=None, with_token=True):
    peer = Peer(name=name)
    _db.session.add(peer)
    _db.session.flush()
    if granted_collection is not None:
        _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=granted_collection.id))
    raw = None
    if with_token:
        raw = generate_token()
        _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()
    return peer, raw


def _auth(raw):
    return {"Authorization": f"Bearer {raw}"}


def _login_as(client, username="admin"):
    """
    Put `username` into the client's session, then PROVE it authenticated.

    The proof matters (2026-08-08). This helper previously just wrote session
    keys and returned; if flask_login declined to load the user, the next
    assertion saw an unauthenticated response and — because the accepted status
    sets included the unauthenticated code — read it as an authorization
    result. test_admin_peer_management_requires_admin passed for two weeks
    without ever reaching the role gate it exists to test.

    So: assert the precondition. A broken login now fails HERE, saying so,
    instead of impersonating a passing authorization test downstream.
    """
    from app.extensions import login_manager

    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None, f"no such user to log in as: {username!r}"

    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        # Flask-Login's session protection compares a per-request identifier
        # against session["_id"]. Absent, "basic" mode marks the session stale
        # and "strong" mode clears it outright. Setting it makes this helper
        # independent of which mode is configured.
        gen = getattr(login_manager, "_session_identifier_generator", None)
        if callable(gen):
            try:
                sess["_id"] = gen()
            except Exception:
                pass   # best effort — the assertion below is the real gate

    me = client.get("/api/auth/me")
    assert me.status_code == 200, (
        f"session login as {username!r} did not authenticate: "
        f"/api/auth/me returned {me.status_code}. The test harness is broken, "
        f"not the endpoint under test."
    )
    return user


# ── token resolution ──────────────────────────────────────────────────────────

def test_resolve_valid_token(app):
    col, _ = _make_collection_with_rec()
    peer, raw = _make_peer(granted_collection=col)
    resolved = resolve_peer_token(raw)
    assert resolved is not None
    token, resolved_peer = resolved
    assert resolved_peer.id == peer.id


def test_resolve_unknown_token(app):
    assert resolve_peer_token("not-a-real-token") is None
    assert resolve_peer_token("") is None
    assert resolve_peer_token(None) is None


def test_resolve_revoked_token(app):
    peer, raw = _make_peer()
    tok = _db.session.query(PeerToken).filter_by(peer_id=peer.id).first()
    tok.revoked_at = _utcnow()
    _db.session.commit()
    assert resolve_peer_token(raw) is None


def test_resolve_token_of_revoked_peer(app):
    peer, raw = _make_peer()
    peer.revoked_at = _utcnow()
    _db.session.commit()
    assert resolve_peer_token(raw) is None


# ── invite validity ───────────────────────────────────────────────────────────

def test_invite_valid_expired_consumed(app):
    peer, _ = _make_peer(with_token=False)
    fresh = PeerInvite(peer_id=peer.id, code_hash=hash_secret("a"),
                       expires_at=_utcnow() + timedelta(days=1))
    expired = PeerInvite(peer_id=peer.id, code_hash=hash_secret("b"),
                         expires_at=_utcnow() - timedelta(days=1))
    consumed = PeerInvite(peer_id=peer.id, code_hash=hash_secret("c"),
                          expires_at=_utcnow() + timedelta(days=1),
                          consumed_at=_utcnow())
    _db.session.add_all([fresh, expired, consumed])
    _db.session.commit()
    assert fresh.is_valid() is True
    assert expired.is_valid() is False
    # Reusable as of 2026-08-30 — consumed no longer means invalid, only expiry does.
    assert consumed.is_valid() is True


# ── access scoping ────────────────────────────────────────────────────────────

def test_granted_peer_can_access(app):
    col, rec = _make_collection_with_rec()
    peer, _ = _make_peer(granted_collection=col)
    track = _db.session.query(Track).filter_by(recording_id=rec.id).first()
    assert peer_can_access_recording(peer, rec) is True
    assert peer_can_access_track(peer, track) is True
    assert peer_granted_collection_ids(peer) == {col.id}


def test_ungranted_peer_denied(app):
    col, rec = _make_collection_with_rec()
    peer, _ = _make_peer(granted_collection=None)   # no grant at all
    track = _db.session.query(Track).filter_by(recording_id=rec.id).first()
    assert peer_can_access_recording(peer, rec) is False
    assert peer_can_access_track(peer, track) is False


def test_grant_to_other_empty_collection_denies(app):
    col, rec = _make_collection_with_rec()          # rec lives in `col`
    other = Collection(name="Empty Other")          # peer granted THIS, which is empty
    _db.session.add(other)
    _db.session.commit()
    peer, _ = _make_peer(granted_collection=other)
    assert peer_can_access_recording(peer, rec) is False


def test_revoking_grant_removes_access(app):
    col, rec = _make_collection_with_rec()
    peer, _ = _make_peer(granted_collection=col)
    assert peer_can_access_recording(peer, rec) is True
    grant = _db.session.query(CollectionGrant).filter_by(peer_id=peer.id).first()
    grant.revoked_at = _utcnow()
    _db.session.commit()
    assert peer_can_access_recording(peer, rec) is False


# ── enrollment handshake ──────────────────────────────────────────────────────

def test_enroll_mints_working_token_and_is_reusable(app):
    peer, _ = _make_peer(with_token=False)
    raw_code = generate_invite_code()
    _db.session.add(PeerInvite(peer_id=peer.id, code_hash=hash_secret(raw_code),
                               expires_at=_utcnow() + timedelta(days=1)))
    _db.session.commit()
    client = app.test_client()

    # First enroll succeeds and returns a token that works on /me
    resp = client.post("/api/share/enroll", json={"invite_code": raw_code})
    assert resp.status_code == 201
    token = resp.get_json()["token"]
    assert token

    me = client.get("/api/share/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.get_json()["peer_name"] == peer.name

    # Reusable as of 2026-08-30 -- a second device can enroll with the SAME
    # code and gets its own, independent token.
    resp2 = client.post("/api/share/enroll", json={"invite_code": raw_code})
    assert resp2.status_code == 201
    token2 = resp2.get_json()["token"]
    assert token2 and token2 != token

    me2 = client.get("/api/share/me", headers=_auth(token2))
    assert me2.status_code == 200
    assert me2.get_json()["peer_name"] == peer.name

    assert len(peer.tokens) == 2


def test_enroll_rejects_bad_code(app):
    client = app.test_client()
    assert client.post("/api/share/enroll", json={"invite_code": "nope"}).status_code == 401
    assert client.post("/api/share/enroll", json={}).status_code == 400


# ── peer-facing browse scoping ────────────────────────────────────────────────

def test_share_browse_requires_token(app):
    client = app.test_client()
    assert client.get("/api/share/me").status_code == 401
    assert client.get("/api/share/collections", headers=_auth("garbage")).status_code == 401


def test_share_collections_scoped(app):
    col, rec = _make_collection_with_rec()
    ungranted = Collection(name="Not Yours")
    _db.session.add(ungranted)
    _db.session.commit()
    peer, raw = _make_peer(granted_collection=col)
    client = app.test_client()

    listing = client.get("/api/share/collections", headers=_auth(raw))
    assert listing.status_code == 200
    ids = {c["id"] for c in listing.get_json()}
    assert ids == {col.id}

    assert client.get(f"/api/share/collections/{col.id}", headers=_auth(raw)).status_code == 200
    assert client.get(f"/api/share/collections/{ungranted.id}", headers=_auth(raw)).status_code == 403


def test_share_recording_and_stream_authorization(app):
    col, rec = _make_collection_with_rec()
    peer_ok, raw_ok = _make_peer(name="Granted", granted_collection=col)
    peer_no, raw_no = _make_peer(name="Ungranted", granted_collection=None)
    track = _db.session.query(Track).filter_by(recording_id=rec.id).first()
    app.config["LIBRARY_ROOT"] = "/tmp/flux-nonexistent-library"
    client = app.test_client()

    # Recording detail: granted 200, ungranted 403
    assert client.get(f"/api/share/recordings/{rec.id}", headers=_auth(raw_ok)).status_code == 200
    assert client.get(f"/api/share/recordings/{rec.id}", headers=_auth(raw_no)).status_code == 403

    # Stream: ungranted 403 (never touches the transcoder); granted passes auth
    # and 404s because the seed FLAC doesn't exist on disk (proves auth cleared).
    assert client.get(f"/api/share/stream/{track.id}", headers=_auth(raw_no)).status_code == 403
    assert client.get(f"/api/share/stream/{track.id}", headers=_auth(raw_ok)).status_code == 404


# ── structural isolation ──────────────────────────────────────────────────────

def test_peer_token_cannot_reach_editing_endpoint(app, seeded_ids):
    """A peer Bearer token is worthless on the local editing door — those
    endpoints are @login_required (cookie), a completely separate identity."""
    _peer, raw = _make_peer()
    client = app.test_client()
    rid = seeded_ids["recording_id"]
    # PUT with only a peer token → the local door rejects it. flask_login either
    # 401s or 302-redirects to the login view depending on config; either way the
    # edit is refused and the peer token bought nothing.
    resp = client.put(f"/api/recordings/{rid}", headers=_auth(raw),
                      json={"notes": "peer tried to edit"})
    assert resp.status_code in (401, 302, 403)
    # And the edit did NOT take effect.
    rec = _db.session.get(Recording, rid)
    assert rec.notes != "peer tried to edit"


def test_admin_peer_management_requires_admin(app):
    # No session at all → login_required blocks. Since 2026-08-08 the local
    # door answers JSON 401 rather than redirecting to a POST-only login route
    # (see tests/test_unauthorized_responses.py), so this is exact now.
    assert app.test_client().get("/api/peers/").status_code == 401

    # A non-admin session → admin_required's ROLE gate, which is the thing this
    # test exists to prove. _login_as asserts the session really authenticated,
    # so a 403 here can only mean "logged in, wrong role" — it can no longer be
    # an unauthenticated response wearing a passing costume.
    listener = User(username="listener1", role="listener", is_active=True, password_hash="x")
    _db.session.add(listener)
    _db.session.commit()
    client = app.test_client()
    _login_as(client, username="listener1")

    assert client.get("/api/peers/").status_code == 403


def test_admin_end_to_end_peer_flow(app):
    """Admin creates a peer, grants a collection, mints an invite; that invite
    then enrolls through the peer door and can browse the granted collection."""
    col, rec = _make_collection_with_rec()
    client = app.test_client()
    _login_as(client)

    created = client.post("/api/peers/", json={"name": "Matt", "contact_note": "bluegrass"})
    assert created.status_code == 201
    peer_id = created.get_json()["id"]

    granted = client.post(f"/api/peers/{peer_id}/grants", json={"collection_ids": [col.id]})
    assert granted.status_code == 200
    assert col.id in granted.get_json()["added"]

    minted = client.post(f"/api/peers/{peer_id}/invites", json={})
    assert minted.status_code == 201
    code = minted.get_json()["code"]
    assert code

    # Now switch to the peer door with that code
    peer_client = app.test_client()
    enrolled = peer_client.post("/api/share/enroll", json={"invite_code": code})
    assert enrolled.status_code == 201
    token = enrolled.get_json()["token"]

    listing = peer_client.get("/api/share/collections", headers=_auth(token))
    assert listing.status_code == 200
    assert {c["id"] for c in listing.get_json()} == {col.id}
