"""Update notifier: version compare, skip, 24h throttle, endpoints, no network."""
import json
import time

import pytest

from app.extensions import db
from app.utils import update_check as uc
from version import __version__


def test_version_compare():
    assert uc.is_newer("0.2.5", "0.2.4")
    assert not uc.is_newer("0.2.4", "0.2.5")
    assert uc.is_newer("0.2.10", "0.2.9")
    assert not uc.is_newer("0.2.4", "0.2.4")
    assert not uc.is_newer("0.2.4.0", "0.2.4")
    assert uc.is_newer("v1.0.0", "0.9.9")
    assert uc.parse_version("garbage") is None
    assert not uc.is_newer("garbage", "0.2.4")


def _manifest(version="99.0.0"):
    return {"version": version, "published": "2026-10-15T00:00:00Z",
            "notes_url": "https://github.com/x/y/releases/tag/v" + version,
            "downloads": {"mac_arm": "https://example.com/arm.dmg",
                          "mac_intel": "https://example.com/intel.dmg",
                          "windows": None},
            "min_version": None}


@pytest.fixture()
def fetch(monkeypatch):
    calls = []
    state = {"m": _manifest()}

    def fake(url):
        calls.append(url)
        return state["m"]
    monkeypatch.setattr(uc, "fetch_manifest", fake)
    monkeypatch.setattr(uc, "_platform_key", lambda: "mac_arm")
    fake.calls, fake.state = calls, state
    return fake


def test_real_fetch_refused_under_testing(app):
    with app.app_context():
        with pytest.raises(RuntimeError):
            uc.fetch_manifest("https://trellismusiclibrary.com/releases/latest.json")


def test_background_not_started_under_testing(app):
    assert uc.start_background(app) is None


def test_check_throttled_to_24h(app, fetch):
    with app.app_context():
        r = uc.check()
        assert r["version"] == "99.0.0" and r["download_url"].endswith("arm.dmg")
        uc.check()
        assert len(fetch.calls) == 1
        uc._set(uc.LAST_CHECK_KEY, str(time.time() - uc.CHECK_INTERVAL_S - 5))
        db.session.commit()
        uc.check()
        assert len(fetch.calls) == 2
        uc.check(force=True)
        assert len(fetch.calls) == 3


def test_disabled_skips_fetch(app, fetch):
    with app.app_context():
        uc.set_enabled(False)
        uc.check()
        assert fetch.calls == []


def test_failure_is_silent(app, monkeypatch):
    def boom(url):
        raise OSError("offline")
    monkeypatch.setattr(uc, "fetch_manifest", boom)
    with app.app_context():
        assert uc.check(force=True) is None
        assert uc.status()["available"] is False


def test_skip_logic(app, fetch):
    with app.app_context():
        uc.check(force=True)
        assert uc.status()["available"] is True
        uc.skip_version("99.0.0")
        s = uc.status()
        assert s["available"] is False and s["skipped"] is True
        fetch.state["m"] = _manifest("99.0.1")
        uc.check(force=True)
        assert uc.status()["available"] is True
        with pytest.raises(ValueError):
            uc.skip_version("nope")


def test_older_manifest_not_available(app, fetch):
    fetch.state["m"] = _manifest("0.0.1")
    with app.app_context():
        uc.check(force=True)
        s = uc.status()
        assert s["available"] is False and s["current"] == __version__


def test_non_https_urls_dropped(app, fetch):
    m = _manifest()
    m["downloads"]["mac_arm"] = "http://example.com/arm.dmg"
    m["notes_url"] = "javascript:alert(1)"
    fetch.state["m"] = m
    with app.app_context():
        r = uc.check(force=True)
        assert r["download_url"] is None and r["notes_url"] is None


# ── endpoints ────────────────────────────────────────────────────────────────

def _login(app, role):
    from app.models.user import User
    from app.extensions import login_manager
    user = db.session.query(User).filter_by(role=role).first()
    if user is None:
        user = User(username=f"u_{role}", role=role, is_active=True, password_hash="x")
        db.session.add(user)
        db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        gen = getattr(login_manager, "_session_identifier_generator", None)
        if callable(gen):
            try:
                sess["_id"] = gen()
            except Exception:
                pass
    assert client.get("/api/auth/me").status_code == 200
    return client


def test_endpoints_require_login(app):
    c = app.test_client()
    assert c.get("/api/system/update").status_code == 401
    assert c.post("/api/system/update/check").status_code == 401
    assert c.post("/api/system/update/skip", json={"version": "1.0.0"}).status_code == 401


def test_endpoints_require_admin(app):
    c = _login(app, "listener")
    assert c.get("/api/system/update").status_code == 403
    assert c.post("/api/system/update/check").status_code == 403
    assert c.post("/api/system/update/skip", json={"version": "1.0.0"}).status_code == 403
    assert c.put("/api/system/update", json={"enabled": False}).status_code == 403


def test_endpoint_payload_and_flow(app, fetch):
    c = _login(app, "admin")
    r = c.get("/api/system/update")
    assert r.status_code == 200
    assert set(r.json) == {"current", "latest", "available", "notes_url",
                           "download_url", "skipped", "enabled"}
    assert r.json["available"] is False and r.json["current"] == __version__

    r = c.post("/api/system/update/check").json
    assert r["available"] is True and r["latest"] == "99.0.0"
    assert r["download_url"] == "https://example.com/arm.dmg"
    assert "/" not in r["current"] and "Users" not in json.dumps(r)

    r = c.post("/api/system/update/skip", json={"version": "99.0.0"}).json
    assert r["available"] is False and r["skipped"] is True
    assert c.post("/api/system/update/skip", json={"version": "x"}).status_code == 400

    assert c.put("/api/system/update", json={"enabled": False}).json["enabled"] is False
    assert c.put("/api/system/update", json={"enabled": "no"}).status_code == 400
