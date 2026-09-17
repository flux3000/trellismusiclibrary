"""
tests/test_remotes.py — outbound sharing: joining and proxying a remote library.

No real network and no real keychain. The HTTP opener and the three keychain
functions are monkeypatched, which is the point: these tests are about THIS
side's behaviour — validation, the allowlist, URL rewriting, error mapping —
not about whether a remote Flux node works. That is what scripts/probe_peer_door.py
is for, and it runs against a real node B.

Writing to the real OS keychain from a test suite would be genuinely bad
manners, so set_remote_token is replaced everywhere below.
"""

import io
import json
import urllib.error

import pytest

from app.extensions import db as _db
from app.models.remote_node import RemoteNode
from app.models.user import User


# ── Harness ───────────────────────────────────────────────────────────────────

def _login_admin(client):
    user = _db.session.query(User).filter_by(role="admin").first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    assert client.get("/api/auth/me").status_code == 200, "admin login failed"


class _FakeResponse:
    """Minimal stand-in for what urlopen returns."""
    def __init__(self, body, status=200, headers=None):
        self._buf = io.BytesIO(body if isinstance(body, bytes) else body.encode())
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self, n=-1):
        return self._buf.read() if n == -1 else self._buf.read(n)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def keychain(monkeypatch):
    """In-memory replacement for the OS keychain."""
    store = {}
    monkeypatch.setattr("app.api.remotes.set_remote_token",
                        lambda node_id, token: store.__setitem__(node_id, token))
    monkeypatch.setattr("app.api.remotes.get_remote_token",
                        lambda node_id: store.get(node_id))
    monkeypatch.setattr("app.api.remotes.delete_remote_token",
                        lambda node_id: store.pop(node_id, None))
    return store


def _patch_open(monkeypatch, handler):
    monkeypatch.setattr("app.api.remotes._opener.open",
                        lambda req, timeout=None: handler(req))


# ── Enrollment ────────────────────────────────────────────────────────────────

def test_enroll_accepts_the_compound_invite_string(app, keychain, monkeypatch):
    def handler(req):
        assert req.full_url == "http://127.0.0.1:5758/api/share/enroll"
        return _FakeResponse(json.dumps({
            "token": "secret-token", "node_name": "Node B",
            "owner_name": "Test Owner", "peer_name": "Node A"}))
    _patch_open(monkeypatch, handler)

    c = app.test_client()
    _login_admin(c)
    r = c.post("/api/remotes/enroll", json={"invite": "http://127.0.0.1:5758#CODE123"})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["display_name"] == "Node B"
    assert body["base_url"] == "http://127.0.0.1:5758"
    assert body["has_token"] is True

    # The token is in the keychain, NOT in the database.
    node = _db.session.get(RemoteNode, body["id"])
    assert keychain[node.id] == "secret-token"
    assert not hasattr(node, "my_token")
    assert "secret-token" not in json.dumps(body)


def test_enroll_rejects_a_malformed_invite(app, keychain):
    c = app.test_client()
    _login_admin(c)
    r = c.post("/api/remotes/enroll", json={"invite": "no-hash-here"})
    assert r.status_code == 400


def test_enroll_rejects_a_non_http_address(app, keychain):
    c = app.test_client()
    _login_admin(c)
    r = c.post("/api/remotes/enroll", json={"invite": "file:///etc/passwd#CODE"})
    assert r.status_code == 400


def test_enroll_passes_through_the_remote_refusal(app, keychain, monkeypatch):
    """A consumed or expired invite should say so, not 'something went wrong'."""
    def handler(req):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {},
            io.BytesIO(json.dumps({"error": "Invalid or expired invite"}).encode()))
    _patch_open(monkeypatch, handler)

    c = app.test_client()
    _login_admin(c)
    r = c.post("/api/remotes/enroll", json={"invite": "http://127.0.0.1:5758#BAD"})
    assert r.status_code == 400
    assert "Invalid or expired invite" in r.get_json()["error"]


def test_enroll_is_refused_when_the_keychain_is_unavailable(app, monkeypatch):
    """No keychain, no enrollment — committing the row anyway would leave an
    unopenable library and a spent invite."""
    def handler(req):
        return _FakeResponse(json.dumps({"token": "t", "node_name": "Node B"}))
    _patch_open(monkeypatch, handler)
    monkeypatch.setattr("app.api.remotes.set_remote_token",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("OS keychain unavailable")))

    c = app.test_client()
    _login_admin(c)
    r = c.post("/api/remotes/enroll", json={"invite": "http://127.0.0.1:5758#CODE"})
    assert r.status_code == 500
    assert _db.session.query(RemoteNode).count() == 0, "left a row behind"


def test_enroll_twice_is_a_conflict(app, keychain, monkeypatch):
    _patch_open(monkeypatch, lambda req: _FakeResponse(
        json.dumps({"token": "t", "node_name": "Node B"})))
    c = app.test_client()
    _login_admin(c)
    first = c.post("/api/remotes/enroll", json={"invite": "http://127.0.0.1:5758#A"})
    assert first.status_code == 201
    second = c.post("/api/remotes/enroll", json={"invite": "http://127.0.0.1:5758#B"})
    assert second.status_code == 409


# ── The proxy ─────────────────────────────────────────────────────────────────

def _joined(app, keychain, monkeypatch):
    _patch_open(monkeypatch, lambda req: _FakeResponse(
        json.dumps({"token": "tok", "node_name": "Node B"})))
    c = app.test_client()
    _login_admin(c)
    r = c.post("/api/remotes/enroll", json={"invite": "http://127.0.0.1:5758#CODE"})
    return c, r.get_json()["id"]


def test_proxy_refuses_paths_outside_the_allowlist(app, keychain, monkeypatch):
    c, node_id = _joined(app, keychain, monkeypatch)

    def handler(req):
        raise AssertionError("must not reach the network for a refused path")
    _patch_open(monkeypatch, handler)

    for path in ("peers/", "auth/me", "admin", "../../etc/passwd"):
        r = c.get(f"/api/remotes/{node_id}/{path}")
        assert r.status_code in (403, 404), f"{path} returned {r.status_code}"


def test_proxy_attaches_the_bearer_token(app, keychain, monkeypatch):
    c, node_id = _joined(app, keychain, monkeypatch)
    seen = {}

    def handler(req):
        seen["auth"] = req.get_header("Authorization")
        seen["url"] = req.full_url
        return _FakeResponse(json.dumps([{"id": 1, "name": "Shared Box"}]))
    _patch_open(monkeypatch, handler)

    r = c.get(f"/api/remotes/{node_id}/collections")
    assert r.status_code == 200
    assert seen["auth"] == "Bearer tok"
    assert seen["url"] == "http://127.0.0.1:5758/api/share/collections"


def test_proxy_rewrites_share_urls_to_local_proxy_urls(app, keychain, monkeypatch):
    """The remote hands out paths on ITS box. Unrewritten, the player and every
    <img> resolve against localhost and 404."""
    c, node_id = _joined(app, keychain, monkeypatch)
    _patch_open(monkeypatch, lambda req: _FakeResponse(json.dumps({
        "id": 5,
        "tracks": [{"id": 9, "stream_url": "/api/share/stream/9"}],
        "images": [{"url": "/api/share/artists/images/3"}],
        "unrelated": "/api/recordings/1",
    })))

    body = c.get(f"/api/remotes/{node_id}/recordings/5").get_json()
    assert body["tracks"][0]["stream_url"] == f"/api/remotes/{node_id}/stream/9"
    assert body["images"][0]["url"] == f"/api/remotes/{node_id}/artists/images/3"
    # Only /api/share/ prefixes are touched.
    assert body["unrelated"] == "/api/recordings/1"


def test_proxy_maps_a_revoked_token_to_401(app, keychain, monkeypatch):
    c, node_id = _joined(app, keychain, monkeypatch)

    def handler(req):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                     io.BytesIO(b'{"error":"Invalid or revoked token"}'))
    _patch_open(monkeypatch, handler)

    r = c.get(f"/api/remotes/{node_id}/collections")
    assert r.status_code == 401
    assert "no longer recognises" in r.get_json()["error"]


def test_proxy_maps_an_unreachable_remote_to_502(app, keychain, monkeypatch):
    c, node_id = _joined(app, keychain, monkeypatch)

    def handler(req):
        raise urllib.error.URLError("Connection refused")
    _patch_open(monkeypatch, handler)

    r = c.get(f"/api/remotes/{node_id}/collections")
    assert r.status_code == 502


def test_proxy_without_a_stored_token_is_409_not_an_empty_library(app, keychain, monkeypatch):
    """A missing credential must never look like 'they shared nothing with you'."""
    c, node_id = _joined(app, keychain, monkeypatch)
    keychain.clear()
    r = c.get(f"/api/remotes/{node_id}/collections")
    assert r.status_code == 409
    assert r.get_json()["error"]


def test_proxy_streams_non_json_through(app, keychain, monkeypatch):
    c, node_id = _joined(app, keychain, monkeypatch)
    _patch_open(monkeypatch, lambda req: _FakeResponse(
        b"ID3fake-mp3-bytes", status=200,
        headers={"Content-Type": "audio/mpeg", "Accept-Ranges": "bytes"}))

    r = c.get(f"/api/remotes/{node_id}/stream/9")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "audio/mpeg"
    assert r.headers["Accept-Ranges"] == "bytes"
    assert r.get_data() == b"ID3fake-mp3-bytes"


def test_proxy_forwards_the_range_header(app, keychain, monkeypatch):
    """Seeking in a long show depends on this."""
    c, node_id = _joined(app, keychain, monkeypatch)
    seen = {}

    def handler(req):
        seen["range"] = req.get_header("Range")
        return _FakeResponse(b"partial", status=206,
                             headers={"Content-Type": "audio/mpeg",
                                      "Content-Range": "bytes 100-200/500"})
    _patch_open(monkeypatch, handler)

    r = c.get(f"/api/remotes/{node_id}/stream/9", headers={"Range": "bytes=100-200"})
    assert seen["range"] == "bytes=100-200"
    assert r.status_code == 206
    assert r.headers["Content-Range"] == "bytes 100-200/500"


# ── Leaving, and the door ─────────────────────────────────────────────────────

def test_leaving_drops_the_keychain_token(app, keychain, monkeypatch):
    c, node_id = _joined(app, keychain, monkeypatch)
    assert keychain.get(node_id) == "tok"

    r = c.delete(f"/api/remotes/{node_id}")
    assert r.status_code == 200
    assert node_id not in keychain, "left a live credential in the keychain"
    assert c.get("/api/remotes/").get_json() == []


@pytest.mark.parametrize("method,path", [
    ("get",    "/api/remotes/"),
    ("post",   "/api/remotes/enroll"),
    ("delete", "/api/remotes/1"),
    ("get",    "/api/remotes/1/collections"),
])
def test_remotes_require_an_admin_session(app, method, path):
    """Consuming someone else's library is admin-only, and a peer Bearer token
    must buy nothing here — this is the LOCAL door."""
    c = app.test_client()
    assert getattr(c, method)(path).status_code == 401
    assert getattr(c, method)(path, headers={"Authorization": "Bearer anything"}).status_code == 401
