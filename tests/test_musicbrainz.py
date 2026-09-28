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


# ── Release lookup (Studio Records spec v1, section 2 — independent review) ──
#
# apply_to_recording()/classify_release()/search_release() cover the fixes
# from the 2026-09-27 independent review: B1 (a shared Performance's date
# fields never get written), B2 (month/day only fill under a matching
# year), B3 (malformed/zero-padded partial dates), S5 (the exception path
# still sets mb_release_status), S6 (the score gate runs on release GROUPS,
# not individual editions), N3 (quotes escaped in the Lucene query).

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.recording_event import RecordingEvent
from app.models.track import Track


def _studio_recording(label, year=1977, month=None, day=None,
                      titles=("A", "", ""), perf=None, artist=None):
    """One studio Recording with its own Artist/Performance/Tracks, unless
    `perf`/`artist` are passed in to attach it to an existing one (the only
    way a shared Performance happens post-B1: app/api/ingest.py itself never
    creates one for a studio recording any more)."""
    if artist is None:
        artist = Artist(name=f"{label} Band")
        _db.session.add(artist)
        _db.session.flush()
    if perf is None:
        perf = Performance(artist_id=artist.id, start_year=year,
                           start_month=month, start_day=day)
        _db.session.add(perf)
        _db.session.flush()
    rec = Recording(performance_id=perf.id, source="SBD", is_complete=True,
                    is_official=False, folder_path=f"{label}/x",
                    is_published=True, kind="studio", title=f"{label} Album")
    _db.session.add(rec)
    _db.session.flush()
    for i, t in enumerate(titles, start=1):
        _db.session.add(Track(recording_id=rec.id, track_number=i, title=t,
                              duration=100, file_path=f"{i:02d}.flac"))
    _db.session.commit()
    return artist, perf, rec


def _release(date="1977-05-08", n=3, mbid="m1", release_group_id="rg"):
    return {"mbid": mbid, "release_group_id": release_group_id, "release_type": "Album",
            "label": "L", "catalog_number": "C", "date": date, "country": "US",
            "track_count": n, "score": 100,
            "tracks": [{"position": i, "title": f"MB{i}"} for i in range(1, n + 1)]}


def test_apply_to_recording_fills_year_only_release_leaves_month_day_null(app):
    """A year-only release date fills the year and stops -- month/day stay
    null rather than the caller guessing at them."""
    a, p, r = _studio_recording("y", year=None)
    mb.apply_to_recording(r, _release(date="1977"))
    _db.session.commit()
    assert (p.start_year, p.start_month, p.start_day) == (1977, None, None)


def test_apply_to_recording_shared_performance_b1(app):
    """B1: a Performance already shared by two studio recordings (the shape
    every studio ingest used to create before this review; app/api/ingest.py
    itself never creates one this way any more, but scripts, old data and
    direct calls like this one still can) never gets its date fields
    written by either recording's release lookup -- the first album's
    release day must not leak onto the second album that happens to share
    its Performance row."""
    a, p, r1 = _studio_recording("s1", year=1977)
    _, _, r2 = _studio_recording("s2", year=1977, perf=p, artist=a)
    assert r1.performance_id == r2.performance_id

    mb.apply_to_recording(r1, _release(date="1977-05-08"))
    _db.session.commit()

    assert (p.start_month, p.start_day) == (None, None), \
        "album 2's shared Performance got album 1's release day"
    note = (_db.session.query(RecordingEvent)
           .filter_by(recording_id=r1.id, event_type="mb_release_matched")
           .first())
    assert "skipped: shared performance" in (note.note or "")


def test_apply_to_recording_two_distinct_performances_both_fill(app):
    """The B1 fix in app/api/ingest.py (a studio ingest always gets its own
    Performance) means two same-year studio albums normally do NOT share a
    row at all -- each one's release lookup fills its OWN Performance, and
    filling one never touches the other."""
    a, p1, r1 = _studio_recording("d1", year=1977)
    _, p2, r2 = _studio_recording("d2", year=1977, artist=a)
    assert p1.id != p2.id

    mb.apply_to_recording(r1, _release(date="1977-05-08"))
    _db.session.commit()

    assert (p1.start_month, p1.start_day) == (5, 8)
    assert (p2.start_month, p2.start_day) == (None, None), \
        "a release date fill on one album's Performance touched the other's"


def test_apply_to_recording_reissue_year_mismatch_b2(app):
    """B2: the collector tagged 1977; the matched release is a 1990
    reissue. The year is kept (already set), but month/day must NOT be
    filled from an edition dated a different year -- that fabricates a date
    ('1977-03-15') that never existed for the original release."""
    a, p, r = _studio_recording("re", year=1977)
    mb.apply_to_recording(r, _release(date="1990-03-15"))
    _db.session.commit()
    assert (p.start_year, p.start_month, p.start_day) == (1977, None, None)


def test_apply_to_recording_matching_year_still_fills_month_day(app):
    """The B2 guard is about a YEAR MISMATCH, not about refusing every fill
    once a year is set -- when the release's own year agrees with the one
    already on the Performance, month/day still fill normally."""
    a, p, r = _studio_recording("match", year=1977)
    mb.apply_to_recording(r, _release(date="1977-05-08"))
    _db.session.commit()
    assert (p.start_year, p.start_month, p.start_day) == (1977, 5, 8)


@pytest.mark.parametrize("date_str, expected", [
    ("1977-00-00", (1977, None, None)),
    ("1977-13-40", (1977, None, None)),
    ("1977-13-01", (1977, None, None)),
    ("1977-05-32", (1977, None, None)),
    ("1977-05-08", (1977, 5, 8)),
])
def test_apply_to_recording_malformed_dates_b3(app, date_str, expected):
    """B3: a zero or out-of-range month/day parses as None for that field
    (and anything after it), never as a literal 0 or an impossible date."""
    a, p, r = _studio_recording("d" + date_str.replace("-", "_"), year=None)
    mb.apply_to_recording(r, _release(date=date_str))
    _db.session.commit()
    assert (p.start_year, p.start_month, p.start_day) == expected


def test_try_match_release_exception_path_sets_status_s5(app, monkeypatch):
    """S5: an exception inside try_match_release() must still set
    mb_release_status (to 'error') and mb_release_checked_at -- leaving it
    NULL means enqueue_followups() re-selects and retries this recording on
    every single boot, forever."""
    a, p, r = _studio_recording("exc")
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "search_release",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = mb.try_match_release(r)
    _db.session.commit()
    assert out == "error"
    assert r.mb_release_status == "error"
    assert r.mb_release_checked_at is not None


def test_classify_release_groups_by_release_group_s6():
    """S6: three editions of ONE album (same release_group_id, each scoring
    near the top the way MusicBrainz's relative scoring actually behaves)
    must match -- gating on individual releases would see three
    near-identical scores and call it ambiguous."""
    editions = [
        {"mbid": "us-cd", "release_group_id": "rg-1", "score": 100, "track_count": 10, "date": "1977-05-08"},
        {"mbid": "uk-lp", "release_group_id": "rg-1", "score": 98,  "track_count": 10, "date": "1977-06-01"},
        {"mbid": "remaster", "release_group_id": "rg-1", "score": 95, "track_count": 10, "date": "2005-01-01"},
    ]
    status, winner, ranked = mb.classify_release(editions, track_count=10)
    assert status == "matched"
    assert winner["release_group_id"] == "rg-1"
    assert winner["mbid"] == "us-cd"          # track count matches exactly
    assert ranked == editions


def test_classify_release_two_different_albums_ambiguous_s6():
    """S6: two candidates from DIFFERENT release groups with a close margin
    is the genuinely ambiguous case (wrong-album risk), unlike same-group
    editions above."""
    candidates = [
        {"mbid": "a", "release_group_id": "rg-a", "score": 100, "track_count": 10},
        {"mbid": "b", "release_group_id": "rg-b", "score": 92,  "track_count": 10},
    ]
    status, winner, _ranked = mb.classify_release(candidates, track_count=10)
    assert status == "ambiguous"
    assert winner is None


def test_search_release_escapes_quotes_n3():
    """N3: a title/artist containing a literal quote must not produce a
    malformed Lucene query -- 'Say \"Hi\"' used to come out as
    release:"Say "Hi"", which MusicBrainz rejects with a 400 the breaker
    then counts as a failure."""
    seen = {}
    orig = mb._get
    mb._get = lambda path, params: (seen.update(params) or {"releases": []})
    try:
        mb.search_release('The "Wailers"', 'Say "Hi"')
    finally:
        mb._get = orig
    query = seen.get("query", "")
    assert '\\"Wailers\\"' in query
    assert '\\"Hi\\"' in query
    # and no bare unescaped quote pairs sitting where the artist/title go
    assert 'artist:"The \\"Wailers\\""' in query
    assert 'release:"Say \\"Hi\\""' in query
