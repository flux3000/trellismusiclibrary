"""
tests/test_user_profile.py — display name and picture.

Two names on purpose (Ryan, 2026-08-25): `username` is the credential,
`display_name` is what a human sees. Ryan's collecting partner signed
everything "oldindian" and was called Jeff.

The credential became editable on 2026-08-28 — see the last section. It had
been fixed for the life of the account, which meant a name typed once on the
first-run setup page could never be corrected.

Both travel: the name a peer sees on a library they joined comes from the
owner's display name, and the picture is served through the share door.
"""

import io
import os

import pytest

from app.extensions import db as _db
from app.models.user import User


@pytest.fixture()
def client(app, tmp_path):
    """
    A FRESH avatar directory per test.

    The first version of this fixture pointed every test at one folder inside
    the repo. A leftover file from an earlier run then made the orphan test
    below fail against code that was actually correct — an hour chasing a bug
    that was in the harness. Same family as the cached-identity trap in
    conftest: shared state between tests reports the wrong component broken.
    """
    from tests.test_peer_sharing import _login_as
    app.config["AVATAR_DIR"] = str(tmp_path / "avatars")
    c = app.test_client()
    _login_as(c)
    return c


def _png():
    """A real 1x1 PNG — small, but genuinely an image file."""
    return io.BytesIO(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae4260 82"
        .replace(" ", "")))


# ── The two names ────────────────────────────────────────────────────────────

def test_name_falls_back_to_the_login_handle(app):
    u = _db.session.query(User).filter_by(username="admin").first()
    u.display_name = None
    assert u.name == "admin", "a blank display name must never show as blank"


def test_display_name_wins_when_set(app):
    u = _db.session.query(User).filter_by(username="admin").first()
    u.display_name = "Jeff"
    assert u.name == "Jeff"
    assert u.username == "admin", "the credential must not change"


def test_setting_and_clearing_a_display_name(client):
    r = client.patch("/api/auth/me", json={"display_name": "  Jeff  "})
    assert r.status_code == 200
    assert r.get_json()["display_name"] == "Jeff", "whitespace is trimmed"
    assert r.get_json()["name"] == "Jeff"

    # Clearing goes back to the handle, and stores NULL rather than ""
    r = client.patch("/api/auth/me", json={"display_name": "   "})
    assert r.get_json()["display_name"] is None
    assert r.get_json()["name"] == "admin"


def test_an_absurd_name_is_refused(client):
    assert client.patch("/api/auth/me",
                        json={"display_name": "x" * 200}).status_code == 400


# ── The picture ──────────────────────────────────────────────────────────────

def test_upload_serve_and_delete(client):
    assert client.get("/api/auth/me").get_json()["has_avatar"] is False
    assert client.get("/api/auth/me/avatar").status_code == 404

    r = client.post("/api/auth/me/avatar",
                    data={"image": (_png(), "me.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 201
    assert r.get_json()["has_avatar"] is True
    assert r.get_json()["avatar_url"]

    served = client.get("/api/auth/me/avatar")
    assert served.status_code == 200
    assert served.mimetype == "image/png"

    assert client.delete("/api/auth/me/avatar").status_code == 200
    assert client.get("/api/auth/me").get_json()["has_avatar"] is False
    assert client.get("/api/auth/me/avatar").status_code == 404


def test_a_non_image_is_refused_by_extension(client):
    r = client.post("/api/auth/me/avatar",
                    data={"image": (io.BytesIO(b"not an image"), "resume.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert "pdf" in r.get_json()["error"].lower()


def test_an_empty_file_is_refused(client):
    r = client.post("/api/auth/me/avatar",
                    data={"image": (io.BytesIO(b""), "me.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 400


def test_replacing_a_png_with_a_jpg_leaves_no_orphan(client, app):
    """A different extension means a different filename. Without an explicit
    unlink the old file survives forever with nothing pointing at it."""
    from pathlib import Path
    client.post("/api/auth/me/avatar", data={"image": (_png(), "me.png")},
                content_type="multipart/form-data")
    d = Path(app.config["AVATAR_DIR"])
    assert len(list(d.glob("user_*"))) == 1

    client.post("/api/auth/me/avatar", data={"image": (_png(), "me.jpg")},
                content_type="multipart/form-data")
    files = list(d.glob("user_*"))
    assert len(files) == 1, f"orphan left behind: {[f.name for f in files]}"
    assert files[0].suffix == ".jpg"

    client.delete("/api/auth/me/avatar")


def test_the_profile_endpoints_require_a_session(app):
    """No session, no profile — this is the front door, not the share door."""
    anon = app.test_client()
    for method, path in [("patch", "/api/auth/me"),
                         ("post", "/api/auth/me/avatar"),
                         ("delete", "/api/auth/me/avatar"),
                         ("get", "/api/auth/me/avatar")]:
        r = getattr(anon, method)(path)
        assert r.status_code in (401, 403), f"{method.upper()} {path} → {r.status_code}"


# ── Changing the sign-in name (2026-08-28) ───────────────────────────────────
#
# Ryan set up a second install, typed "jeff" on the setup page, and got
# `ryanfbaker` — his macOS account name. Two separate faults: the setup page
# discarded the answer when a database already existed (run.py, covered by
# test_first_run_owner_account.py), and once wrong there was no way to fix it.
# This section covers the second half.


def test_the_signin_name_can_be_changed(client):
    r = client.patch("/api/auth/me", json={"username": "oldindian"})
    assert r.status_code == 200
    assert r.get_json()["username"] == "oldindian"
    assert _db.session.query(User).filter_by(username="oldindian").first() is not None


def test_a_rename_does_not_sign_you_out(client):
    """
    Flask-Login carries the row id, not the name. If that ever stopped being
    true, renaming yourself would log you out mid-request and the failure
    would look like a permissions bug rather than a session one.
    """
    assert client.patch("/api/auth/me", json={"username": "oldindian"}).status_code == 200
    after = client.get("/api/auth/me")
    assert after.status_code == 200, "the session must survive its own rename"
    assert after.get_json()["username"] == "oldindian"


def test_an_empty_signin_name_is_refused(client):
    r = client.patch("/api/auth/me", json={"username": "   "})
    assert r.status_code == 400
    assert _db.session.query(User).filter_by(username="admin").first() is not None


def test_an_absurd_signin_name_is_refused(client):
    r = client.patch("/api/auth/me", json={"username": "x" * 65})
    assert r.status_code == 400


def test_a_name_someone_else_holds_is_refused(client):
    _db.session.add(User(username="jeff", password_hash="x", role="user", is_active=True))
    _db.session.commit()
    r = client.patch("/api/auth/me", json={"username": "jeff"})
    assert r.status_code == 409
    assert _db.session.query(User).filter_by(username="admin").first() is not None


def test_renaming_to_your_own_name_is_not_a_conflict(client):
    """The uniqueness check must exclude the row being edited, or saving a
    form without touching the field reports a clash with yourself."""
    assert client.patch("/api/auth/me", json={"username": "admin"}).status_code == 200


def test_a_refused_signin_name_leaves_the_display_name_alone(client):
    """
    Both names travel in one PATCH. Validating as it goes would leave the
    display name changed and the sign-in name not — a half-applied edit the
    person never asked for and cannot see.
    """
    _db.session.add(User(username="jeff", password_hash="x", role="user", is_active=True))
    _db.session.commit()
    r = client.patch("/api/auth/me",
                     json={"username": "jeff", "display_name": "Should Not Stick"})
    assert r.status_code == 409
    me = _db.session.query(User).filter_by(username="admin").first()
    assert me.display_name != "Should Not Stick"


def test_both_names_change_together_when_both_are_valid(client):
    r = client.patch("/api/auth/me",
                     json={"username": "oldindian", "display_name": "Jeff"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["username"] == "oldindian"
    assert body["display_name"] == "Jeff"
    assert body["name"] == "Jeff", "the display name still wins for what humans see"
