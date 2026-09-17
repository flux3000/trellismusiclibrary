"""
tests/test_musicbrainz.py — MusicBrainz matching logic (2026-08-07).

Every test here is NETWORK-FREE. The HTTP layer is stubbed or bypassed, because
a unit suite that reaches musicbrainz.org is slow, rate-limited, and red
whenever the wifi is. What's actually worth testing is the pure logic anyway:
the confidence gate and the response flattening.

The confidence gate is the part most likely to need retuning once real library
names run through it, which is exactly why it lives in its own pure function
(`classify`) rather than inline in the fetch path.
"""

import pytest

from app.utils import musicbrainz as mb


# ── Confidence gate ─────────────────────────────────────────────────────────

def _c(score, name="X"):
    return {"mbid": f"id-{name}-{score}", "name": name, "score": score}


def test_classify_no_candidates_is_none():
    assert mb.classify([]) == ("none", None)


def test_classify_clear_winner_matches():
    status, best = mb.classify([_c(100, "The Meters"), _c(70, "Meters Tribute")])
    assert status == "matched"
    assert best["name"] == "The Meters"


def test_classify_close_runner_up_is_ambiguous():
    """A 100 with a 98 behind it is NOT confident, however high the top looks.

    This is the case the margin exists for — tribute acts, reunions and
    same-named bands all score near-identically on a name query, and picking
    the top one silently attaches wrong facts to a page nobody re-checks.
    """
    status, best = mb.classify([_c(100, "The Meters"), _c(98, "The Meters")])
    assert status == "ambiguous"
    assert best is None


def test_classify_low_top_score_is_ambiguous():
    """Nothing scored well — a weak best guess must not become a fact."""
    assert mb.classify([_c(55), _c(20)]) == ("ambiguous", None)


def test_classify_single_low_candidate_still_ambiguous():
    """Being the ONLY candidate does not make a poor match a good one."""
    assert mb.classify([_c(60)]) == ("ambiguous", None)


def test_classify_single_strong_candidate_matches():
    status, best = mb.classify([_c(97, "Fela Kuti")])
    assert status == "matched"
    assert best["name"] == "Fela Kuti"


# ── Response flattening ─────────────────────────────────────────────────────

_ARTIST = {
    "id": "5f5b1c1a-0000-4000-8000-00000000c1a4",
    "name": "The Meters",
    "type": "Group",
    "score": 100,
    "disambiguation": "US funk band",
    "area": {"name": "United States"},
    "begin-area": {"name": "New Orleans"},
    "life-span": {"begin": "1965", "end": "1977", "ended": True},
}


def test_summarise_flattens_expected_fields():
    s = mb._summarise(_ARTIST)
    assert s["mbid"] == _ARTIST["id"]
    assert s["type"] == "Group"
    assert s["area"] == "New Orleans, United States"   # begin-area first
    assert s["begin"] == "1965"
    assert s["end"] == "1977"
    assert s["ended"] is True
    assert s["disambiguation"] == "US funk band"


def test_area_dedupes_identical_begin_and_area():
    """Many entries repeat the same place in both fields — 'Berlin, Berlin'
    reads like a bug to anyone looking at the page."""
    a = dict(_ARTIST, area={"name": "Berlin"}, **{"begin-area": {"name": "Berlin"}})
    assert mb._area_name(a) == "Berlin"


def test_area_none_when_no_place_known():
    assert mb._area_name({"name": "Someone"}) is None


def test_summarise_tolerates_missing_optional_blocks():
    """Sparse entries are the norm for obscure acts — a bare record must
    flatten to Nones, not raise."""
    s = mb._summarise({"id": "abc", "name": "Rockygrass Thunder Jam"})
    assert s["mbid"] == "abc"
    assert s["type"] is None and s["area"] is None
    assert s["begin"] is None and s["disambiguation"] is None


# ── apply_to_artist ──────────────────────────────────────────────────────

def test_apply_never_touches_human_curated_fields(app, seeded_ids):
    """MusicBrainz may fill its own columns and nothing else.

    name/bio/genre/members are Ryan's. An external database silently rewriting
    a hand-corrected act name would be the same class of bug as the AI Assist
    auto-apply that was removed in July.
    """
    from app.extensions import db as _db
    from app.models.artist import Artist

    p = _db.session.get(Artist, seeded_ids["artist_id"])
    p.bio = "Hand-written bio"
    original_name = p.name
    _db.session.commit()

    mb.apply_to_artist(p, mb._summarise(_ARTIST), {"wikipedia": "http://x"})
    _db.session.commit()

    assert p.name == original_name
    assert p.bio == "Hand-written bio"
    assert p.mbid == _ARTIST["id"]
    assert p.mb_type == "Group"
    assert p.mb_status == "matched"
    assert "wikipedia" in p.mb_links_json


# ── Safety rails ────────────────────────────────────────────────────────────

def test_lookups_disabled_under_testing(app):
    """The suite must never make a network call. `enabled()` is False under
    TESTING, which is what keeps resolve_or_create_artist() offline in every
    other test file that happens to create a Artist."""
    with app.app_context():
        assert mb.enabled() is False


def test_try_match_is_noop_when_disabled(app, seeded_ids):
    """Returns None and leaves mb_status NULL — 'never looked up', so the row
    is retried later rather than being recorded as a real 'no match'."""
    from app.extensions import db as _db
    from app.models.artist import Artist

    p = _db.session.get(Artist, seeded_ids["artist_id"])
    p.mb_status = None
    assert mb.try_match_artist(p) is None
    assert p.mb_status is None


def test_circuit_breaker_trips_and_resets():
    """Offline, every call burns the full timeout — a 40-show import would
    otherwise spend minutes waiting on DNS that will never answer."""
    mb.reset_breaker()
    assert not mb.tripped()
    for _ in range(mb._MAX_CONSECUTIVE_FAILURES):
        mb._failures[0] += 1
    assert mb.tripped()
    # A tripped breaker short-circuits without attempting a request.
    assert mb._get("artist/", {"query": "anything"}) is None
    mb.reset_breaker()
    assert not mb.tripped()


def test_search_artist_empty_name_returns_empty_without_calling(monkeypatch):
    called = []
    monkeypatch.setattr(mb, "_get", lambda *a, **k: called.append(1))
    assert mb.search_artist("") == []
    assert mb.search_artist("   ") == []
    assert called == []
