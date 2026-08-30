"""
tests/test_venue_list_uncapped.py — GET /api/venues/ must return every venue,
not just the first 200 alphabetically.

Ryan reported 2026-08-30: the Venues list in the sidebar's bottom-left nav
"stops midway through the alphabet at L". list_venues() (app/api/venues.py)
carried a `.limit(200)` that list_performers/list_artists/list_genres —
the sidebar's three sibling dimension endpoints — never had; ordered by
`Venue.name`, a 200-row cut lands exactly where a library with more than 200
venues would appear to run out partway through the alphabet, with nothing on
screen to say anything was missing.
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
    # 250 venues, so a name starting past "L" alphabetically exists beyond
    # row 200 — the exact shape of the bug Ryan saw.
    with app.app_context():
        for i in range(250):
            _db.session.add(Venue(name=f"Venue {i:04d}"))
        _db.session.commit()
        total = _db.session.query(Venue).count()

    res = client.get("/api/venues/")
    assert res.status_code == 200
    names = [v["name"] for v in res.get_json()]
    assert len(names) == total, (
        f"expected all {total} venues, got {len(names)} — the 200-row cap is back")
    assert "Venue 0249" in names
