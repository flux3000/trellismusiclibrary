"""
tests/test_enroll_rate_limit.py — the one unauthenticated peer-door route must
not accept unlimited attempts.

Closes a TODO carried since July: `/api/share/enroll` is the only route on the
share door callable with no credentials, because a peer holding an invite has
no token yet. The invite code is unguessable, but "unguessable" assumes a
bounded number of guesses and nothing bounded them.

The subtle half of this is WHO gets counted. Behind a Cloudflare Tunnel,
cloudflared connects from 127.0.0.1, so every visitor on earth shares one
socket address — a naive limiter would let the first bot lock out every real
peer. See app/utils/rate_limit.py.
"""

import os
import tempfile

import pytest

from config import Config
from app import create_app
from app.extensions import db as _db
from app.utils import rate_limit


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    class _Cfg(Config):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"
        TESTING = True
        DEV_MODE = False
        ENROLL_RATE_LIMIT = 3
        ENROLL_RATE_WINDOW = 900

    application = create_app(config_class=_Cfg)
    rate_limit.reset()                 # module-level state; isolate every test
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
    rate_limit.reset()
    os.unlink(path)


def _try(client, code="nope", **kw):
    return client.post("/api/share/enroll", json={"invite_code": code}, **kw)


# ── The limit itself ─────────────────────────────────────────────────────────

def test_attempts_are_refused_past_the_limit(app):
    c = app.test_client()
    for i in range(3):
        assert _try(c).status_code == 401, f"attempt {i+1} should reach the invite check"
    r = _try(c)
    assert r.status_code == 429
    assert r.get_json()["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) > 0


def test_a_refused_attempt_does_not_extend_the_block(app):
    """
    A caller who keeps hammering must not permanently re-arm their own block.
    Recording refused attempts turns a speed bump into a lifetime ban earned by
    a script someone forgot to stop, so refusals are not counted.
    """
    c = app.test_client()
    for _ in range(3):
        _try(c)
    first = int(_try(c).headers["Retry-After"])
    for _ in range(20):
        _try(c)
    assert int(_try(c).headers["Retry-After"]) <= first


def test_the_limit_can_be_disabled(app):
    app.config["ENROLL_RATE_LIMIT"] = 0
    c = app.test_client()
    for _ in range(30):
        assert _try(c).status_code == 401


# ── Who gets counted ─────────────────────────────────────────────────────────

def test_callers_are_counted_separately_by_address(app):
    c = app.test_client()
    for _ in range(3):
        _try(c, environ_overrides={"REMOTE_ADDR": "203.0.113.1"})
    assert _try(c, environ_overrides={"REMOTE_ADDR": "203.0.113.1"}).status_code == 429
    # A different caller is unaffected.
    assert _try(c, environ_overrides={"REMOTE_ADDR": "203.0.113.2"}).status_code == 401


def test_forwarded_header_is_used_when_configured_and_peer_is_loopback(app):
    """The tunnel case: one socket address, many real visitors."""
    app.config["TRUSTED_CLIENT_IP_HEADER"] = "CF-Connecting-IP"
    c = app.test_client()
    for _ in range(3):
        _try(c, headers={"CF-Connecting-IP": "198.51.100.7"})
    assert _try(c, headers={"CF-Connecting-IP": "198.51.100.7"}).status_code == 429
    # Same socket, different real visitor — must NOT be locked out.
    assert _try(c, headers={"CF-Connecting-IP": "198.51.100.8"}).status_code == 401


def test_forwarded_header_is_ignored_when_not_configured(app):
    """An install with no proxy in front of it must not trust a header it was
    never told to expect — otherwise the limiter is bypassed by adding one."""
    c = app.test_client()
    for i in range(3):
        _try(c, headers={"CF-Connecting-IP": f"198.51.100.{i}"})
    assert _try(c, headers={"CF-Connecting-IP": "198.51.100.99"}).status_code == 429


def test_forwarded_header_is_ignored_from_a_non_loopback_peer(app):
    """A header is only as trustworthy as the peer that sent it. Someone on the
    LAN talking to us directly does not get to name their own bucket."""
    app.config["TRUSTED_CLIENT_IP_HEADER"] = "CF-Connecting-IP"
    c = app.test_client()
    lan = {"REMOTE_ADDR": "192.168.1.50"}
    for i in range(3):
        _try(c, headers={"CF-Connecting-IP": f"198.51.100.{i}"},
             environ_overrides=lan)
    assert _try(c, headers={"CF-Connecting-IP": "198.51.100.99"},
                environ_overrides=lan).status_code == 429


# ── Everything else on the door is unaffected ────────────────────────────────

def test_the_rest_of_the_share_door_is_not_rate_limited(app):
    """Only enroll wears the decorator. A token-bearing peer browsing a library
    makes hundreds of calls and must never be throttled by this."""
    c = app.test_client()
    for _ in range(30):
        assert c.get("/api/share/me").status_code == 401
