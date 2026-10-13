"""Top Shelf criteria from Settings (2026-10-13): A+/A, SBD, FM, favorites,
chosen collections; one pool; SBD/FM-only recordings played in the last 90
days go to the back."""
from datetime import datetime, timedelta

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.collection import Collection, CollectionRecording
from app.models.performance import Performance
from app.models.play_log import PlayLog
from app.models.recording import Recording
from app.models.track import Track
from app.utils import top_shelf


def _rec(name, **kw):
    a = Artist(name=f"{name} Act")
    _db.session.add(a); _db.session.flush()
    p = Performance(artist_id=a.id, start_year=2000, start_month=1, start_day=1)
    _db.session.add(p); _db.session.flush()
    r = Recording(performance_id=p.id, folder_path=f"{name}/x", kind=kw.pop("kind", "live"), **kw)
    _db.session.add(r); _db.session.flush()
    _db.session.add(Track(recording_id=r.id, track_number=1, title="t", file_path="01.flac"))
    _db.session.commit()
    return r


def _ids(user_id=None):
    recs, demoted = top_shelf.pool(user_id, Recording.query)
    return {r.id for r in recs}, demoted


def test_defaults_are_grades_sbd_fm(app):
    assert top_shelf.get_settings(None) == top_shelf.DEFAULTS
    a = _rec("A", quality="A", source="AUD")
    sbd = _rec("S", quality="C", source="sbd")
    fm = _rec("F", source="FM")
    aud = _rec("U", quality="B", source="AUD")
    album = _rec("L", quality="A+", source="SBD", kind="studio")
    hidden = _rec("H", quality="A", source="SBD", is_published=False)
    ids, _ = _ids()
    assert {a.id, sbd.id, fm.id} <= ids
    assert not ids & {aud.id, album.id, hidden.id}


def test_settings_round_trip_and_criteria(app):
    from app.models.user import User
    u = User.query.first() or User(username="u", role="admin")
    if u.id is None:
        u.set_password("x"); _db.session.add(u); _db.session.commit()
    fav = _rec("Fav", source="AUD", is_favorite=True)
    col_rec = _rec("Col", source="AUD")
    sbd = _rec("S", source="SBD")
    c = Collection(name="Box"); _db.session.add(c); _db.session.flush()
    _db.session.add(CollectionRecording(collection_id=c.id, recording_id=col_rec.id, order=0))
    _db.session.commit()
    s = top_shelf.set_settings(u.id, {"sbd": False, "fm": False, "top_grades": False,
                                      "favorites": True, "collections": True,
                                      "collection_ids": [c.id, "x"]})
    assert s["collection_ids"] == [c.id]
    ids, _ = _ids(u.id)
    assert ids == {fav.id, col_rec.id}
    top_shelf.set_settings(u.id, {k: False for k in ("top_grades", "sbd", "fm", "favorites", "collections")})
    assert _ids(u.id)[0] == set()


def test_recent_sbd_only_play_is_demoted_not_dropped(app):
    from app.models.user import User
    u = User.query.first()
    if u is None:
        u = User(username="u2", role="admin"); u.set_password("x"); _db.session.add(u); _db.session.commit()
    fresh, recent, old, graded = (_rec("Fr", source="SBD"), _rec("Re", source="SBD"),
                                  _rec("Ol", source="SBD"), _rec("Gr", source="SBD", quality="A"))
    for r, days in ((recent, 3), (old, 200), (graded, 3)):
        _db.session.add(PlayLog(user_id=u.id, track_id=r.tracks[0].id,
                                played_at=datetime.utcnow() - timedelta(days=days)))
    _db.session.commit()
    ids, demoted = _ids(u.id)
    assert {fresh.id, recent.id, old.id, graded.id} <= ids
    assert demoted == {recent.id}            # graded is on the shelf through A, not chance


def test_preferences_api_carries_top_shelf(app):
    from tests.test_studio_library_payloads import _login_as
    c = app.test_client(); _login_as(c)
    got = c.get("/api/preferences").get_json()["top_shelf"]
    assert got["top_grades"] and got["sbd"] and got["fm"]
    put = c.put("/api/preferences", json={"top_shelf": {"favorites": True}}).get_json()["top_shelf"]
    assert put["favorites"] and put["sbd"]


def test_peer_draw_uses_host_settings(app):
    from app.models.user import User
    u = User.query.first()
    if u is None:
        u = User(username="h", role="admin"); u.set_password("x"); _db.session.add(u); _db.session.commit()
    fav = _rec("PFav", source="AUD", is_favorite=True)
    fm = _rec("PFm", source="FM")
    graded = _rec("PGr", source="AUD", quality="A")
    top_shelf.set_settings(u.id, {"top_grades": False, "sbd": False, "fm": True, "favorites": True})
    _db.session.add(PlayLog(user_id=u.id, track_id=fm.tracks[0].id, played_at=datetime.utcnow()))
    _db.session.commit()
    recs, demoted = top_shelf.pool(u.id, Recording.query, for_peer=True)
    ids = {r.id for r in recs}
    assert {fm.id, fav.id} <= ids and graded.id not in ids
    assert demoted == set()
