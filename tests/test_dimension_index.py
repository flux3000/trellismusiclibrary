"""tests/test_dimension_index.py -- the ?index=1 form of the five list endpoints.

The dimension index pages are rows that carry an excerpt, a year span, who
played there and a run of artist faces. Those fields come from
app/utils/dim_index.py behind `?index=1`, so the sidebar's every-render call
stays light. Pinned here:

  * the default payload is unchanged (no extras leak into the sidebar call)
  * each extra is right, and uses only text already stored
  * a peer's extras are computed over the VISIBLE recordings only
  * the number of queries does not grow with the number of rows
"""

import json

import pytest
from sqlalchemy import event

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.artist_image import ArtistImage
from app.models.collection import Collection, CollectionRecording
from app.models.event import Event
from app.models.genre import Genre
from app.models.musician import Musician, Membership
from app.models.peer import Peer, CollectionGrant, PeerToken
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.venue import Venue
from app.utils import dim_index
from app.utils.peer_auth import generate_token, hash_secret


@pytest.fixture()
def api(app):
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


def _rec(perf, n=1, **kw):
    out = []
    for i in range(n):
        r = Recording(performance_id=perf.id, source="SBD", is_complete=True,
                      is_official=False, kind="live", is_published=True,
                      folder_path=f"idx/{perf.id}/{i}", **kw)
        _db.session.add(r)
        out.append(r)
    _db.session.flush()
    return out


def _world():
    """Two acts in one genre, one shared venue, one event."""
    jazz = Genre(name="Jazz", color="#6a7fb5")
    _db.session.add(jazz)
    _db.session.flush()
    a1 = Artist(name="Alpha Trio", genre_id=jazz.id, mb_area="Seattle",
                bio="<cite index=\"1-1\">Alpha</cite> Trio played jazz [2].")
    a2 = Artist(name="Beta Quartet", genre_id=jazz.id,
                dossier_json=json.dumps({"biography": "Legacy dossier text."}))
    _db.session.add_all([a1, a2])
    _db.session.flush()
    m = Musician(name="Pat Player", bio="Plays guitar.")
    _db.session.add(m)
    _db.session.flush()
    _db.session.add(Membership(artist_id=a1.id, musician_id=m.id, order=0,
                               instrument="guitar, banjo"))
    _db.session.add(Membership(artist_id=a2.id, musician_id=m.id, order=0,
                               instrument="guitar"))
    v = Venue(name="The Hall", city="Oakland", state="CA", country="US",
              history="A hall with a history.")
    _db.session.add(v)
    ev = Event(name="Summer Fest")
    _db.session.add(ev)
    _db.session.flush()
    p1 = Performance(artist_id=a1.id, venue_id=v.id, event_id=ev.id,
                     start_year=1975, start_month=6, start_day=3)
    p2 = Performance(artist_id=a2.id, venue_id=v.id, event_id=ev.id,
                     start_year=1979, start_month=6, start_day=5)
    _db.session.add_all([p1, p2])
    _db.session.flush()
    r1 = _rec(p1, 2)
    r2 = _rec(p2, 1)
    _db.session.add(ArtistImage(artist_id=a1.id, filename="a.jpg", ext=".jpg",
                                is_primary=True, sort_order=0))
    _db.session.commit()
    return dict(a1=a1, a2=a2, m=m, v=v, ev=ev, genre=jazz, r1=r1, r2=r2)


# ── excerpt ──────────────────────────────────────────────────────────────────

def test_excerpt_strips_citations_and_cuts_at_a_word():
    raw = "<cite index=\"3-1\">Band</cite> formed in 1971 [1, 2]. " + "word " * 200
    out = dim_index.excerpt(raw, limit=60)
    assert "<cite" not in out and "[1" not in out
    assert out.endswith("…") and len(out) <= 61
    assert dim_index.excerpt(None) == "" and dim_index.excerpt("  ") == ""


# ── default payload is untouched ─────────────────────────────────────────────

@pytest.mark.parametrize("path,extra", [
    ("/api/artists/", "excerpt"), ("/api/musicians/", "instruments"),
    ("/api/venues/", "excerpt"), ("/api/events/", "top_artists"),
    ("/api/genres/", "faces"),
])
def test_default_list_has_no_index_fields(api, app, path, extra):
    _world()
    rows = api.get(path).get_json()
    assert rows and all(extra not in r for r in rows)


# ── local extras ─────────────────────────────────────────────────────────────

def test_artist_index_fields(api, app):
    w = _world()
    rows = {r["id"]: r for r in api.get("/api/artists/?index=1").get_json()}
    a = rows[w["a1"].id]
    assert a["excerpt"] == "Alpha Trio played jazz."
    assert a["area"] == "Seattle"
    assert (a["first_year"], a["last_year"]) == (1975, 1975)
    assert a["image_id"]                                  # the base field still wins
    # no bio: the stored legacy dossier is the fallback, nothing is fetched
    assert rows[w["a2"].id]["excerpt"] == "Legacy dossier text."
    assert rows[w["a2"].id]["recording_count"] == 1       # base fields intact


def test_musician_index_fields(api, app):
    w = _world()
    row = next(r for r in api.get("/api/musicians/?index=1").get_json()
               if r["id"] == w["m"].id)
    assert row["instruments"] == ["guitar", "banjo"]
    assert row["artists"] == ["Alpha Trio", "Beta Quartet"]   # busiest first
    assert row["artist_count"] == 2 and row["recording_count"] == 3
    assert row["excerpt"] == "Plays guitar."
    assert "image_id" not in row                          # musician rows draw no picture


def test_venue_index_fields(api, app):
    w = _world()
    row = next(r for r in api.get("/api/venues/?index=1").get_json()
               if r["id"] == w["v"].id)
    assert "top_artists" not in row and "artist_count" not in row   # rows list no artists
    assert (row["first_year"], row["last_year"]) == (1975, 1979)
    assert row["excerpt"] == "A hall with a history."     # history when no bio


def test_event_index_fields_derive_from_performances(api, app):
    w = _world()
    row = next(r for r in api.get("/api/events/?index=1").get_json()
               if r["id"] == w["ev"].id)
    assert row["first"] == [1975, 6, 3] and row["last"] == [1979, 6, 5]
    assert row["show_venue"] == "The Hall"
    assert row["show_place"] == "Oakland, CA"
    assert row["top_artists"] == ["Alpha Trio", "Beta Quartet"]


def test_event_index_reads_every_performance_not_just_recorded(api, app):
    w = _world()
    v2 = Venue(name="The Barn", city="Fresno", state="CA")
    a3 = Artist(name="Gamma Band")
    _db.session.add_all([v2, a3])
    _db.session.flush()
    # No recordings: still part of the event's dates, venues and artists.
    _db.session.add(Performance(artist_id=a3.id, venue_id=v2.id, event_id=w["ev"].id,
                                start_year=1979, start_month=6, start_day=7,
                                end_year=1979, end_month=6, end_day=9))
    _db.session.commit()
    row = next(r for r in api.get("/api/events/?index=1").get_json()
               if r["id"] == w["ev"].id)
    assert row["first"] == [1975, 6, 3] and row["last"] == [1979, 6, 9]
    assert row["venues"] == ["The Hall, Oakland", "The Barn, Fresno"]
    assert row["venue_count"] == 2
    assert row["top_artists"] == ["Alpha Trio", "Beta Quartet", "Gamma Band"]
    assert row["artist_count"] == 3


def test_genre_faces_prefer_photographed_acts(api, app):
    w = _world()
    row = api.get("/api/genres/?index=1").get_json()[0]
    assert [f["name"] for f in row["faces"]] == ["Alpha Trio", "Beta Quartet"]
    assert row["faces"][0]["image_id"] and row["faces"][1]["image_id"] is None


# ── no N+1 ───────────────────────────────────────────────────────────────────

def _count(fn):
    n = [0]

    def tick(*a, **k):
        n[0] += 1
    event.listen(_db.engine, "before_cursor_execute", tick)
    try:
        fn()
    finally:
        event.remove(_db.engine, "before_cursor_execute", tick)
    return n[0]


def _grow(k):
    g = Genre(name=f"G{k}")
    _db.session.add(g)
    _db.session.flush()
    for i in range(k):
        a = Artist(name=f"Act {k}-{i}", genre_id=g.id, bio="x")
        v = Venue(name=f"Venue {k}-{i}", city="C")
        m = Musician(name=f"Man {k}-{i}")
        e = Event(name=f"Ev {k}-{i}")
        _db.session.add_all([a, v, m, e])
        _db.session.flush()
        _db.session.add(Membership(artist_id=a.id, musician_id=m.id, order=0))
        p = Performance(artist_id=a.id, venue_id=v.id, event_id=e.id, start_year=2000)
        _db.session.add(p)
        _db.session.flush()
        _rec(p)
    _db.session.commit()


@pytest.mark.parametrize("path", ["/api/artists/", "/api/musicians/",
                                  "/api/venues/", "/api/genres/"])
def test_index_extras_cost_a_constant_number_of_queries(api, app, path):
    """The base list may still lazy-load per row (artists: members and genre);
    what ?index=1 ADDS must not scale with the rows."""
    def extra():
        plain = _count(lambda: api.get(path))
        indexed = _count(lambda: api.get(path + "?index=1"))
        return indexed - plain

    _grow(3)
    api.get(path)                                         # warm
    small = extra()
    _grow(25)
    big = extra()
    assert big <= small + 1, (path, small, big)


# ── peer scoping ─────────────────────────────────────────────────────────────

@pytest.fixture()
def peer_world(app):
    w = _world()
    col = Collection(name="Shared")
    _db.session.add(col)
    _db.session.flush()
    _db.session.add(CollectionRecording(collection_id=col.id,
                                        recording_id=w["r1"][0].id, order=0))
    peer = Peer(name="Matt")
    _db.session.add(peer)
    _db.session.flush()
    _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=col.id))
    raw = generate_token()
    _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()
    w["hdr"] = {"Authorization": f"Bearer {raw}"}
    return w


def test_peer_venue_extras_cover_visible_recordings_only(app, peer_world):
    w = peer_world
    rows = app.test_client().get("/api/share/venues/?index=1", headers=w["hdr"]).get_json()
    row = next(r for r in rows if r["id"] == w["v"].id)
    assert "top_artists" not in row and "artist_count" not in row
    assert (row["first_year"], row["last_year"]) == (1975, 1975)
    assert row["recording_count"] == 1                    # one of the two granted-act recordings


def test_peer_musician_extras_name_visible_acts_only(app, peer_world):
    w = peer_world
    rows = app.test_client().get("/api/share/musicians/?index=1", headers=w["hdr"]).get_json()
    row = next(r for r in rows if r["id"] == w["m"].id)
    assert row["artists"] == ["Alpha Trio"] and row["artist_count"] == 1
    assert row["instruments"] == ["guitar", "banjo"]      # Beta's stint is not read


def test_peer_artist_extras_and_image(app, peer_world):
    w = peer_world
    rows = app.test_client().get("/api/share/artists/?index=1", headers=w["hdr"]).get_json()
    assert [r["name"] for r in rows] == ["Alpha Trio"]
    assert rows[0]["excerpt"] == "Alpha Trio played jazz."
    assert rows[0]["image_id"] and rows[0]["genre_color"] == "#6a7fb5"
    assert (rows[0]["first_year"], rows[0]["last_year"]) == (1975, 1975)


def test_peer_genre_faces_exclude_hidden_acts(app, peer_world):
    w = peer_world
    rows = app.test_client().get("/api/share/genres/?index=1", headers=w["hdr"]).get_json()
    assert [f["name"] for f in rows[0]["faces"]] == ["Alpha Trio"]


def test_peer_default_lists_carry_no_index_fields(app, peer_world):
    w = peer_world
    c = app.test_client()
    assert "excerpt" not in c.get("/api/share/artists/", headers=w["hdr"]).get_json()[0]
    assert "faces" not in c.get("/api/share/genres/", headers=w["hdr"]).get_json()[0]
