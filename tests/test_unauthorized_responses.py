"""
tests/test_unauthorized_responses.py — what an unauthenticated caller gets.

Added 2026-08-08 after a peer-door probe reported that a peer Bearer token had
reached the local admin API. It hadn't. flask_login was 302-ing to `login_view`
(auth.login, a POST-only route); nothing matched GET there, so Flask fell
through to serve_frontend's catch-all and returned index.html with status 200.

A failed authentication that answers 200 is a trap for every client:
  * the frontend only noticed because res.json() choked on HTML
  * a probe read it as a security hole
  * the milestone-2 consumer proxy cannot tell failure from success

These tests pin the contract: /api/* answers JSON 401, everything else
redirects to the SPA.
"""

import pytest


@pytest.mark.parametrize("path", [
    "/api/auth/me",
    "/api/artists/1",
    "/api/recordings/1",
    "/api/collections/",
    "/api/peers/",
    "/api/stream/1",
])
def test_api_paths_answer_json_401(app, path):
    r = app.test_client().get(path)
    assert r.status_code == 401, f"{path} answered {r.status_code}, not 401"
    assert r.is_json, f"{path} answered non-JSON — a client cannot parse this"
    assert "error" in r.get_json()


def test_api_401_is_not_html(app):
    """The specific bug: index.html served with a success status."""
    r = app.test_client().get("/api/auth/me")
    body = r.get_data(as_text=True).lstrip().lower()
    assert not body.startswith("<!doctype"), "served the SPA shell instead of an error"
    assert not body.startswith("<html")


def test_write_methods_also_get_401_not_a_redirect(app):
    c = app.test_client()
    for method, path in (("put", "/api/artists/1"),
                         ("delete", "/api/recordings/1"),
                         ("post", "/api/collections/")):
        r = getattr(c, method)(path, json={})
        assert r.status_code == 401, f"{method.upper()} {path} → {r.status_code}"


def test_non_api_paths_redirect_to_the_spa(app):
    """Browser navigation to a protected non-API path should land on the app,
    which shows its own login screen — not a JSON blob."""
    r = app.test_client().get("/some/deep/link", follow_redirects=False)
    assert r.status_code in (200, 302)
    if r.status_code == 302:
        assert r.headers["Location"].endswith("/")


def test_share_door_is_unaffected(app):
    """The peer door has its own 401 from @peer_required and must not start
    redirecting — it never had a cookie session to lose."""
    r = app.test_client().get("/api/share/collections")
    assert r.status_code == 401
    assert r.is_json
