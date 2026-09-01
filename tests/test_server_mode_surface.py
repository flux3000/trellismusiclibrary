"""
tests/test_server_mode_surface.py — SERVER_MODE must serve the share door and
nothing else.

Decided 2026-08-25. Exposure is via a Cloudflare Tunnel, and a tunnel points at
a PORT: every route this process registers is on the public internet. The peer
door (`/api/share/*`) is safe to expose because it is structurally read-only —
there are no write endpoints in that blueprint to find a bug in. The front door
is not: it holds login, delete-with-files, folder moves and the BYOK AI
endpoints.

The guarantee under test is ABSENCE, not refusal. `/api/auth/login` must 404
because Flask never heard of it — not 401, not 302, and above all not 200 with
a page of HTML (this app has shipped that bug once already; see the
`_unauthorized` handler's comment in app/__init__.py).

Every assertion here is paired with the same assertion against a NORMAL app, so
a change that broke route registration outright could not make this file pass.
"""

import os
import tempfile

import pytest

from config import Config, DEV_SECRET_DEFAULT
from app import create_app
from app.extensions import db as _db


# Representative front-door paths — one per blueprint family that must not
# exist in a public process, plus the SPA catch-all and a static asset.
FRONT_DOOR = [
    "/api/auth/me",
    "/api/recordings/1",
    "/api/performers/",
    "/api/collections/",
    "/api/ingest/preview",
    "/api/peers/",
    "/api/remotes/",
    "/api/quality/staging",
    "/api/debug/info",
    "/api/preferences/",
    "/api/search",
    "/api/system/library-status",
    "/static/js/app.js",
    "/",                     # the SPA catch-all
    "/some/deep/spa/route",  # the catch-all's whole point
]


def _make(server_mode):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    class _Cfg(Config):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"
        TESTING = True
        DEV_MODE = False
        SERVER_MODE = server_mode
        # _validate_server_mode refuses the known dev default under SERVER_MODE,
        # which is itself covered below.
        SECRET_KEY = "test-key-not-the-dev-default"

    app = create_app(config_class=_Cfg)
    app._tmp_db_path = path
    return app


@pytest.fixture()
def server_app():
    app = _make(True)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
    os.unlink(app._tmp_db_path)
    # WAL sidecars leak if only the .db is unlinked — see conftest.py.
    for _sidecar in (f"{app._tmp_db_path}-wal", f"{app._tmp_db_path}-shm"):
        try:
            os.unlink(_sidecar)
        except OSError:
            pass


@pytest.fixture()
def normal_app():
    app = _make(False)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
    os.unlink(app._tmp_db_path)
    # WAL sidecars leak if only the .db is unlinked — see conftest.py.
    for _sidecar in (f"{app._tmp_db_path}-wal", f"{app._tmp_db_path}-shm"):
        try:
            os.unlink(_sidecar)
        except OSError:
            pass


# ── The absence guarantee ─────────────────────────────────────────────────────

def test_url_map_holds_only_share_routes(server_app):
    """The strongest form of the claim: nothing outside /api/share is even a
    rule. Stated over the whole url_map rather than a list of paths, so a
    blueprint added later is caught without anyone remembering to list it."""
    strays = sorted(str(r) for r in server_app.url_map.iter_rules()
                    if not str(r).startswith("/api/share"))
    assert strays == [], f"SERVER_MODE registered non-share routes: {strays}"


@pytest.mark.parametrize("path", FRONT_DOOR)
def test_front_door_paths_are_absent(server_app, path):
    """404 specifically. 401 would mean the route exists and refused us; 302
    would mean a redirect to a login flow; 200 would mean the SPA catch-all
    swallowed it. Only 404 means 'never constructed'."""
    r = server_app.test_client().get(path)
    assert r.status_code == 404, (
        f"{path} answered {r.status_code} in SERVER_MODE — expected 404 (absent)"
    )


def test_no_static_folder(server_app):
    """A peer node has its own frontend; this process ships none."""
    assert server_app.static_folder is None


# ── Negative control: the same claims must FAIL against a normal app ─────────
#
# Without these, a bug that stopped registering blueprints entirely would make
# every assertion above pass while the real app was dead.

@pytest.mark.parametrize("path", FRONT_DOOR)
def test_front_door_paths_exist_in_a_normal_app(normal_app, path):
    r = normal_app.test_client().get(path)
    assert r.status_code != 404, (
        f"{path} 404s in a NORMAL app — the absence test above proves nothing"
    )


def test_normal_app_registers_more_than_share(normal_app):
    non_share = [str(r) for r in normal_app.url_map.iter_rules()
                 if not str(r).startswith("/api/share")]
    assert len(non_share) > 50


# ── The peer door itself still works ─────────────────────────────────────────

def test_share_door_is_present_and_demands_a_token(server_app):
    """Not 404 (it exists) and not 200 (it wants a bearer token). If this ever
    404s, SERVER_MODE has stopped serving the one thing it is for."""
    r = server_app.test_client().get("/api/share/me")
    assert r.status_code == 401


def test_enroll_is_reachable_without_credentials(server_app):
    """The one unauthenticated route on the peer door — a peer with an invite
    has no token yet. 400 for a missing code, not 404 and not 401."""
    r = server_app.test_client().post("/api/share/enroll", json={})
    assert r.status_code == 400


# ── The boot guards still hold under the new construction order ──────────────

def test_refuses_to_boot_with_the_dev_secret():
    class _Cfg(Config):
        TESTING = True
        DEV_MODE = False
        SERVER_MODE = True
        SECRET_KEY = DEV_SECRET_DEFAULT

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(config_class=_Cfg)


def test_refuses_to_boot_with_dev_mode_on():
    class _Cfg(Config):
        TESTING = True
        DEV_MODE = True
        SERVER_MODE = True
        SECRET_KEY = "test-key-not-the-dev-default"

    with pytest.raises(RuntimeError, match="DEV_MODE"):
        create_app(config_class=_Cfg)
