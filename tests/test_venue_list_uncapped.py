"""tests/test_venue_list_uncapped.py — GET /api/venues/ must return every venue,
not just the first 200 alphabetically.
"""

import pytest

from app.extensions import db as _db
from app.models.user import User
from app.models.venue import Venue


def _login_as(client, username="admin"):
    from app.extensions import login_manager

    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None, f"no such user to log in as: {username!r}"
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        gen = getattr(login_manager, "_session_identifier_generator", None)
        if callable(gen):
            try:
                sess["_id"] = gen()
            except Exception:
                pass
    me = client.get("/api/auth/me")
    assert me.status_code == 200, (
        f"session login as {username!r} did not authenticate: /api/auth/me "
        f"returned {me.status_code}. The harness is broken, not the endpoint.")
    return user


@pytest.fixture()
def client(app):
    c = app.test_client()
    _login_as(c)
    return c


def test_venue_list_is_not_capped_at_200(app, client):
    with app.app_context():
        for i in range(250):
            _db.session.add(Venue(name=f"Venue {i:04d}"))
        _db.session.commit()
        total = _db.session.query(Venue).count()

    # all=1: these venues have no recording, and the plain list shows only venues with one.
    res = client.get("/api/venues/?all=1")
    assert res.status_code == 200
    names = [v["name"] for v in res.get_json()]
    assert len(names) == total, (
        f"expected all {total} venues, got {len(names)} — the 200-row cap is back")
    assert "Venue 0249" in names
