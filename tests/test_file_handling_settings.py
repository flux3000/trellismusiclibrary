"""
tests/test_file_handling_settings.py -- node_settings.py file-handling helpers
and the /api/system/file-handling endpoints.
"""

import pytest

from app.extensions import db as _db
from app.models.user import User
from app.utils import node_settings


def _login_as(client, username):
    from app.extensions import login_manager
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        gen = getattr(login_manager, "_session_identifier_generator", None)
        if callable(gen):
            try:
                sess["_id"] = gen()
            except Exception:
                pass


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def listener_user(app):
    user = User(username="listener1", role="listener", is_active=True, password_hash="x")
    _db.session.add(user)
    _db.session.commit()
    return user


# ── get_file_handling() defaults ─────────────────────────────────────────────

def test_defaults_when_absent(app):
    result = node_settings.get_file_handling()
    assert result["file_handling_mode"] == "keep"
    assert result["rename_folders"] is False
    assert result["rename_files"] is False
    assert result["naming_scheme"] == "original"
    assert result["naming_template"] == ""
    assert result["write_tags_on_ingest"] is False
    assert result["write_tags_default"] is False
    assert result["placement"] == "artist"


# ── apply_mode() ──────────────────────────────────────────────────────────────

def test_apply_mode_organize_sets_all_switches(app):
    result = node_settings.apply_mode("organize")
    assert result["file_handling_mode"] == "organize"
    assert result["rename_folders"] is True
    assert result["rename_files"] is True
    assert result["naming_scheme"] == "number_title"
    assert result["write_tags_on_ingest"] is True
    assert result["write_tags_default"] is True


def test_apply_mode_back_to_keep_resets_switches(app):
    node_settings.apply_mode("organize")
    node_settings.set_file_handling(rename_files=False)
    result = node_settings.apply_mode("keep")
    assert result["rename_folders"] is False
    assert result["rename_files"] is False
    assert result["naming_scheme"] == "original"


def test_apply_mode_rejects_unknown_mode(app):
    with pytest.raises(ValueError):
        node_settings.apply_mode("sometimes")


def test_switches_stay_individually_editable_after_a_mode_change(app):
    node_settings.apply_mode("organize")
    node_settings.set_file_handling(write_tags_default=False)
    result = node_settings.get_file_handling()
    assert result["file_handling_mode"] == "organize"
    assert result["write_tags_default"] is False
    assert result["write_tags_on_ingest"] is True  # untouched


# ── set_file_handling() validation ──────────────────────────────────────────

def test_set_file_handling_rejects_bad_scheme(app):
    with pytest.raises(ValueError):
        node_settings.set_file_handling(naming_scheme="whatever")


def test_set_file_handling_rejects_non_bool(app):
    with pytest.raises(ValueError):
        node_settings.set_file_handling(rename_files="yes")


def test_set_file_handling_rejects_unknown_key(app):
    with pytest.raises(ValueError):
        node_settings.set_file_handling(file_handling_mode="organize")


def test_set_file_handling_naming_template_round_trips(app):
    node_settings.set_file_handling(naming_scheme="custom",
                                    naming_template="{artist} - {title}")
    result = node_settings.get_file_handling()
    assert result["naming_scheme"] == "custom"
    assert result["naming_template"] == "{artist} - {title}"


# ── Custom template validation (spec section 4/12): a broken template can
# never be written, whether it arrives with the scheme switch or on its own
# once the scheme is already custom.

def test_set_file_handling_rejects_an_unparseable_template():
    with pytest.raises(ValueError):
        node_settings.set_file_handling(naming_scheme="custom",
                                        naming_template="{nonsense}")


def test_set_file_handling_rejects_a_bad_template_added_after_the_scheme(app):
    node_settings.set_file_handling(naming_scheme="custom",
                                    naming_template="{artist} - {title}")
    with pytest.raises(ValueError):
        node_settings.set_file_handling(naming_template="{artist} - {title")
    # The last GOOD template survives the rejected write.
    assert node_settings.get_file_handling()["naming_template"] == "{artist} - {title}"


def test_set_file_handling_allows_a_bad_template_string_when_scheme_is_not_custom(app):
    # naming_template is only READ when naming_scheme is custom (spec 1.1);
    # writing one under a preset scheme is not this call's business to police.
    node_settings.set_file_handling(naming_template="{nonsense}")
    assert node_settings.get_file_handling()["naming_template"] == "{nonsense}"


# ── placement round-trips through file_under_artist_folder ─────────────────

def test_placement_round_trips_through_file_under_artist_folder(app):
    node_settings.set_file_handling(placement="root")
    assert node_settings.file_under_artist_folder() is False
    assert node_settings.get_file_handling()["placement"] == "root"

    node_settings.set_file_handling(placement="artist")
    assert node_settings.file_under_artist_folder() is True
    assert node_settings.get_file_handling()["placement"] == "artist"


def test_set_file_handling_rejects_bad_placement(app):
    with pytest.raises(ValueError):
        node_settings.set_file_handling(placement="somewhere")


# ── PUT /api/system/file-handling ────────────────────────────────────────────

def test_get_file_handling_endpoint(client):
    _login_as(client, "admin")
    resp = client.get("/api/system/file-handling")
    assert resp.status_code == 200
    assert resp.get_json()["file_handling_mode"] == "keep"


def test_put_file_handling_mode(client):
    _login_as(client, "admin")
    resp = client.put("/api/system/file-handling", json={"file_handling_mode": "organize"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["file_handling_mode"] == "organize"
    assert body["rename_files"] is True


def test_put_file_handling_switch(client):
    _login_as(client, "admin")
    client.put("/api/system/file-handling", json={"file_handling_mode": "organize"})
    resp = client.put("/api/system/file-handling", json={"write_tags_default": False})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["write_tags_default"] is False
    assert body["write_tags_on_ingest"] is True


def test_put_file_handling_rejects_bad_scheme(client):
    _login_as(client, "admin")
    resp = client.put("/api/system/file-handling", json={"naming_scheme": "nonsense"})
    assert resp.status_code == 400


def test_put_file_handling_rejects_an_invalid_custom_template(client):
    _login_as(client, "admin")
    resp = client.put("/api/system/file-handling",
                      json={"naming_scheme": "custom", "naming_template": "{nope}"})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_put_file_handling_selects_custom_with_no_template_yet(client):
    """
    R4 (re-review, 2026-09-25): the Settings scheme select saves
    naming_scheme='custom' ALONE, immediately, the moment it's picked --
    the Template field is readonly until the STORED scheme reads 'custom',
    so on a fresh install there is no template to send yet. This must
    succeed (a transitional state), not 400 and revert the select back to
    'Keep original' before the field ever unlocks.
    """
    _login_as(client, "admin")
    resp = client.put("/api/system/file-handling", json={"naming_scheme": "custom"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["naming_scheme"] == "custom"
    assert body["naming_template"] == ""

    # The field is now unlocked and the user types a template -- that save
    # (naming_template alone, scheme already 'custom') still validates it.
    resp2 = client.put("/api/system/file-handling",
                       json={"naming_template": "{artist} - {title}"})
    assert resp2.status_code == 200
    assert resp2.get_json()["naming_template"] == "{artist} - {title}"

    # An actually-invalid template is still rejected once there's a scheme
    # to validate it against.
    resp3 = client.put("/api/system/file-handling",
                       json={"naming_template": "{nope}"})
    assert resp3.status_code == 400


def test_put_file_handling_rejects_empty_body(client):
    _login_as(client, "admin")
    resp = client.put("/api/system/file-handling", json={})
    assert resp.status_code == 400


def test_put_file_handling_admin_gate_is_exactly_403(client, listener_user):
    _login_as(client, "listener1")
    resp = client.put("/api/system/file-handling", json={"file_handling_mode": "organize"})
    assert resp.status_code == 403


def test_get_file_handling_does_not_require_admin(client, listener_user):
    _login_as(client, "listener1")
    resp = client.get("/api/system/file-handling")
    assert resp.status_code == 200
