"""tests/test_security_hardening.py — P0 security hardening:

1. DEV_MODE now defaults OFF (fail-closed) instead of defaulting ON.
2. SERVER_MODE boot guard (app._validate_server_mode) refuses to start when
   SERVER_MODE + DEV_MODE are both on, or when SERVER_MODE is on with a
   default/blank SECRET_KEY.
3. stream_ingest_preview is constrained to a configurable IMPORT_ROOTS
   allowlist, on top of the pre-existing filename-in-folder containment
   check.

These tests build fresh apps via create_app() (not the shared `app` fixture)
where boot behavior itself is under test, and use the `app`/`db` fixtures
from conftest.py (which already forces DEV_MODE=False) for the endpoint
tests.
"""

import pytest

from config import Config, _env_flag, DEV_SECRET_DEFAULT
from app import create_app
from app.extensions import db as _db


# ── _env_flag / DEV_MODE-SERVER_MODE default parsing ──────────────────────

def test_env_flag_unset_uses_default(monkeypatch):
    monkeypatch.delenv("DEV_MODE", raising=False)
    assert _env_flag("DEV_MODE", default=False) is False


def test_env_flag_explicit_true(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    assert _env_flag("DEV_MODE", default=False) is True


def test_env_flag_explicit_false_or_garbage_is_false(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    assert _env_flag("DEV_MODE", default=False) is False
    monkeypatch.setenv("DEV_MODE", "nonsense")
    assert _env_flag("DEV_MODE", default=False) is False


def test_dev_secret_default_constant_value():
    assert DEV_SECRET_DEFAULT == "dev-secret-change-me"


# ── Boot guards ────────────────────────────────────────────────────────────

def test_server_mode_and_dev_mode_both_on_refuses_boot():
    class BadConfig(Config):
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        TESTING = True
        SERVER_MODE = True
        DEV_MODE = True
        SECRET_KEY = "a-real-unique-secret-not-the-default"

    with pytest.raises(RuntimeError, match="DEV_MODE"):
        create_app(config_class=BadConfig)


def test_server_mode_with_default_secret_key_refuses_boot():
    class BadConfig(Config):
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        TESTING = True
        SERVER_MODE = True
        DEV_MODE = False
        SECRET_KEY = DEV_SECRET_DEFAULT

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(config_class=BadConfig)


def test_server_mode_with_blank_secret_key_refuses_boot():
    class BadConfig(Config):
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        TESTING = True
        SERVER_MODE = True
        DEV_MODE = False
        SECRET_KEY = ""

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(config_class=BadConfig)


def test_server_mode_boots_with_dev_mode_off_and_real_secret():
    """The positive case: SERVER_MODE alone, with DEV_MODE off and a
    non-default SECRET_KEY, must boot cleanly."""
    class GoodConfig(Config):
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        TESTING = True
        SERVER_MODE = True
        DEV_MODE = False
        SECRET_KEY = "a-real-unique-secret-not-the-default"

    app = create_app(config_class=GoodConfig)
    assert app.config["SERVER_MODE"] is True
    assert app.config["DEV_MODE"] is False


def test_dev_mode_alone_still_boots():
    """DEV_MODE on its own (SERVER_MODE off, the local-dev case) is fine —
    the guard only fires when both are on."""
    class LocalDevConfig(Config):
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        TESTING = True
        SERVER_MODE = False
        DEV_MODE = True

    app = create_app(config_class=LocalDevConfig)
    assert app.config["DEV_MODE"] is True


# ── DEV_MODE default-off behavior (no auto-login without explicit opt-in) ──

def test_dev_mode_off_no_auto_login_on_protected_route():
    class NoDevConfig(Config):
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        TESTING = True
        DEV_MODE = False

    app = create_app(config_class=NoDevConfig)
    with app.app_context():
        _db.create_all()
        client = app.test_client()
        r = client.get("/api/auth/me")
        # No dev_auto_login before_request handler is registered, and no
        # session cookie was set — flask-login must refuse this request.
        assert r.status_code != 200
        _db.session.remove()
        _db.drop_all()


def test_dev_mode_on_registers_auto_login_admin():
    """Positive control for the gate itself: with DEV_MODE explicitly true
    (opt-in), the pre-existing auto-login-as-first-admin behavior still
    works, proving we didn't break the feature — only its default."""
    class DevOnConfig(Config):
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        TESTING = True
        DEV_MODE = True

    app = create_app(config_class=DevOnConfig)
    with app.app_context():
        _db.create_all()
        from app.models.user import User
        _db.session.add(User(username="admin", role="admin", is_active=True, password_hash="x"))
        _db.session.commit()

        client = app.test_client()
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.get_json()["username"] == "admin"

        _db.session.remove()
        _db.drop_all()


# ── stream_ingest_preview IMPORT_ROOTS allowlist ───────────────────────────

@pytest.fixture()
def preview_client(app):
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


def test_ingest_preview_403_outside_import_roots(preview_client, app, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")

    app.config["IMPORT_ROOTS"] = [str(allowed)]

    r = preview_client.get("/api/stream/ingest-preview", query_string={
        "folder": str(outside), "file": "secret.txt",
    })
    assert r.status_code == 403


def test_ingest_preview_allows_configured_root(preview_client, app, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "track.flac").write_bytes(b"fake-flac-bytes")

    app.config["IMPORT_ROOTS"] = [str(allowed)]

    r = preview_client.get("/api/stream/ingest-preview", query_string={
        "folder": str(allowed), "file": "track.flac",
    })
    assert r.status_code == 200
    assert r.data == b"fake-flac-bytes"


def test_ingest_preview_blocks_traversal_within_allowed_root(preview_client, app, tmp_path):
    """Even when `folder` resolves inside an allowed root, `file` still
    can't traverse out of `folder` itself — the pre-existing containment
    check stays in force as defense in depth."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (tmp_path / "sibling_secret.txt").write_text("nope")

    app.config["IMPORT_ROOTS"] = [str(tmp_path)]  # allow the whole tmp_path

    r = preview_client.get("/api/stream/ingest-preview", query_string={
        "folder": str(allowed), "file": "../sibling_secret.txt",
    })
    assert r.status_code == 403


def test_ingest_preview_missing_params_400(preview_client):
    r = preview_client.get("/api/stream/ingest-preview", query_string={"folder": "/tmp"})
    assert r.status_code == 400
