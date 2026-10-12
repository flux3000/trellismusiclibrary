"""tests/test_genre_color.py: a Genre always has a colour."""
import re
import pytest
from app.extensions import db as _db
from app.models.genre import Genre
from app.utils.genre_color import DEFAULT_GENRE_COLORS as P, default_genre_color
from app.utils.schema_upgrades import ensure_genre_colors
from tests.test_peer_sharing import _login_as

HEX = re.compile(r"^#[0-9a-f]{6}$")


def test_empty_picks_first_palette_colour():
    assert default_genre_color([]) == P[0]
    assert default_genre_color([None, ""]) == P[0]


def test_least_used_wins_and_ties_follow_palette_order():
    assert default_genre_color([P[0]]) == P[1]
    assert default_genre_color(list(P) + [P[0], P[1]]) == P[2]
    assert default_genre_color([P[0], P[0], P[1]] + list(P[3:])) == P[2]
    assert default_genre_color([P[0].upper()]) == P[1]


def test_api_create_assigns_default_and_explicit_wins(app):
    c = app.test_client()
    _login_as(c)
    r = c.post("/api/genres/", json={"name": "A"}).get_json()
    assert r["color"] == P[0]
    r = c.post("/api/genres/", json={"name": "B", "color": ""}).get_json()
    assert r["color"] == P[1]
    r = c.post("/api/genres/", json={"name": "C", "color": "#ABCDEF"}).get_json()
    assert r["color"] == "#abcdef"


def test_api_update_cannot_clear_and_validates(app):
    c = app.test_client()
    _login_as(c)
    gid = c.post("/api/genres/", json={"name": "A"}).get_json()["id"]
    for bad in (None, "", "  ", "red", "#fff"):
        assert c.put(f"/api/genres/{gid}", json={"color": bad}).status_code == 400
    assert _db.session.get(Genre, gid).color == P[0]
    assert c.put(f"/api/genres/{gid}", json={"color": "#112233"}).status_code == 200
    assert _db.session.get(Genre, gid).color == "#112233"


def test_ingest_path_assigns_colour(app):
    from app.api.ingest import _apply_artist_genre
    from app.models.artist import Artist
    a = Artist(name="Act X")
    _db.session.add(a)
    _db.session.flush()
    _apply_artist_genre(a, {"genre_name": "Zydeco"})
    _db.session.commit()
    assert HEX.match(a.genre.color)


def test_musicbrainz_path_assigns_colour(app):
    from app.utils import musicbrainz as mb
    from app.models.artist import Artist
    a = Artist(name="Act Y")
    _db.session.add(a)
    _db.session.flush()
    mb.apply_genre_to_artist(a, ["zydeco"])
    _db.session.commit()
    assert a.genre is not None and HEX.match(a.genre.color)


def test_backfill_fills_only_unset_and_is_idempotent(app):
    _db.session.add_all([Genre(name="Set", color="#112233"),
                         Genre(name="Null1"), Genre(name="Null2")])
    _db.session.commit()
    assert ensure_genre_colors(_db.engine) == 2
    _db.session.expire_all()
    cols = {g.name: g.color for g in _db.session.query(Genre)}
    assert cols["Set"] == "#112233"
    assert cols["Null1"] == P[0] and cols["Null2"] == P[1]
    assert ensure_genre_colors(_db.engine) is None
    _db.session.expire_all()
    assert {g.name: g.color for g in _db.session.query(Genre)} == cols


def test_picker_put_sends_only_color_and_index_reflects_it(app):
    # What the colour circle sends: {color} alone, as a lowercase-able #rrggbb.
    c = app.test_client()
    _login_as(c)
    gid = c.post("/api/genres/", json={"name": "A", "description": "keep"}).get_json()["id"]
    assert c.put(f"/api/genres/{gid}", json={"color": "#AABBCC"}).status_code == 200
    g = _db.session.get(Genre, gid)
    assert g.color == "#aabbcc" and g.name == "A" and g.description == "keep"
    row = next(r for r in c.get("/api/genres/?index=1").get_json() if r["id"] == gid)
    assert row["color"] == "#aabbcc"
