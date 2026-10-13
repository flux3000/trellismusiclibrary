"""Album research proposes the act's genre only when it has none (Ryan, 2026-10-13)."""
from types import SimpleNamespace as NS

from app.lomax.skills import album


def _subject(cur, genre_open):
    return NS(type="folder", current=cur, obj=None,
              scratch={"current": cur, "question": "", "genre_open": genre_open})


def test_genre_kept_when_open(app):
    cur = {"artist": "Béla Fleck", "title": "x", "year": "1999", "tracks": []}
    out, props = album.normalize({"thinking": "", "proposals": [
        {"field": "genre", "proposed": "Bluegrass, Newgrass", "confidence": "high", "source": "web"}]},
        _subject(cur, True))
    assert [p["proposed"] for p in props if p["field"] == "genre"] == ["Bluegrass"]


def test_genre_dropped_when_act_has_one(app):
    cur = {"artist": "Béla Fleck", "genre": "Bluegrass", "title": "x", "year": "1999", "tracks": []}
    out, props = album.normalize({"thinking": "", "proposals": [
        {"field": "genre", "proposed": "Jazz", "confidence": "high", "source": "web"}]},
        _subject(cur, False))
    assert not [p for p in props if p["field"] == "genre"]
