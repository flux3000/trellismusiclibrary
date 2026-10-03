"""
tests/test_mb_release.py -- MusicBrainz RELEASE lookup (Studio Records spec
v1, section 2): search_release(), classify_release(), lookup_release(),
apply_to_recording(), try_match_release(), link_release(), unlink_release(),
the mb_release follow-up kind, and the migration's new columns.

Every test here is NETWORK-FREE, same discipline as tests/test_musicbrainz.py:
_get() is monkeypatched wherever a network round-trip would otherwise happen,
and enabled()-under-TESTING is asserted directly rather than assumed.
"""

import subprocess
import sqlite3
import sys
from pathlib import Path

import pytest

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.recording_event import RecordingEvent
from app.models.track import Track
from app.utils import musicbrainz as mb

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Fixtures: canned MusicBrainz JSON ───────────────────────────────────────

_SEARCH_RESPONSE = {
    "releases": [
        {
            "id": "rel-mbid-1",
            "title": "Head Hunters",
            "score": 95,
            "release-group": {"id": "rg-1", "primary-type": "Album"},
            "label-info": [{"label": {"name": "Columbia"}, "catalog-number": "KC 32731"}],
            "date": "1973-10-13",
            "country": "US",
            "track-count": 4,
            "media": [{"track-count": 4}],
        },
        {
            "id": "rel-mbid-2",
            "title": "Head Hunters (reissue)",
            "score": 60,
            "release-group": {"id": "rg-2", "primary-type": "Album"},
            "label-info": [{"label": {"name": "Sony"}, "catalog-number": "XYZ"}],
            "date": "2000",
            "country": "US",
            "track-count": 4,
        },
    ]
}

_LOOKUP_RESPONSE = {
    "id": "rel-mbid-1",
    "title": "Head Hunters",
    "release-group": {"id": "rg-1", "primary-type": "Album"},
    "label-info": [{"label": {"name": "Columbia"}, "catalog-number": "KC 32731"}],
    "date": "1973-10-13",
    "country": "US",
    "media": [
        {"tracks": [
            {"position": 1, "title": "Chameleon",
             "recording": {"title": "Chameleon"}},
            {"position": 2, "title": "Watermelon Man",
             "recording": {"title": "Watermelon Man"}},
        ]},
        {"tracks": [
            {"position": 1, "title": "Sly",
             "recording": {"title": "Sly"}},
            {"position": 2, "title": "Vein Melter",
             "recording": {"title": "Vein Melter"}},
        ]},
    ],
}


def _candidate(score, track_count=4, name="X"):
    return {"mbid": "id-%s-%s" % (name, score), "title": name, "score": score,
           "track_count": track_count}


# ── classify_release() gate ─────────────────────────────────────────────────

def test_classify_release_no_candidates_is_none():
    assert mb.classify_release([]) == ("none", None, [])


def test_classify_release_clear_winner_matches():
    status, best, ranked = mb.classify_release(
        [_candidate(95, name="A"), _candidate(50, name="B")], track_count=4)
    assert status == "matched"
    assert best["title"] == "A"
    assert len(ranked) == 2


def test_classify_release_close_runner_up_is_ambiguous():
    """95 with a 90 right behind it is not a confident match -- same margin
    rule as the artist gate, for the same reason (two real releases can
    both score well on a name search)."""
    status, best, _ranked = mb.classify_release(
        [_candidate(95, name="A"), _candidate(90, name="B")], track_count=4)
    assert status == "ambiguous"
    assert best is None


def test_classify_release_track_count_mismatch_drops_candidate():
    """A candidate whose track count is more than one off the recording's
    own track count is removed before the gate runs -- here it removes the
    only candidate, so the result is 'none', not a confident 'matched' on a
    release that plainly isn't this one."""
    status, best, ranked = mb.classify_release(
        [_candidate(95, track_count=9, name="A")], track_count=4)
    assert status == "none"
    assert best is None
    assert ranked == []


def test_classify_release_track_count_off_by_one_is_tolerated():
    status, best, _ranked = mb.classify_release(
        [_candidate(95, track_count=5, name="A")], track_count=4)
    assert status == "matched"
    assert best["title"] == "A"


# ── search_release() / lookup_release() -- canned JSON, no network ─────────

def test_search_release_builds_combined_query_and_summarises(monkeypatch):
    seen = {}

    def _fake_get(path, params):
        seen["path"] = path
        seen["params"] = params
        return _SEARCH_RESPONSE

    monkeypatch.setattr(mb, "_get", _fake_get)
    candidates = mb.search_release("Herbie Hancock", "Head Hunters")

    assert seen["path"] == "release/"
    assert "artist:" in seen["params"]["query"]
    assert "release:" in seen["params"]["query"]
    assert "Herbie Hancock" in seen["params"]["query"]
    assert "Head Hunters" in seen["params"]["query"]

    assert len(candidates) == 2
    top = candidates[0]
    assert top["mbid"] == "rel-mbid-1"
    assert top["release_type"] == "Album"
    assert top["label"] == "Columbia"
    assert top["catalog_number"] == "KC 32731"
    assert top["country"] == "US"
    assert top["date"] == "1973-10-13"
    assert top["track_count"] == 4


def test_search_release_empty_inputs_return_empty_without_calling(monkeypatch):
    called = []
    monkeypatch.setattr(mb, "_get", lambda *a, **k: called.append(1))
    assert mb.search_release("", "Head Hunters") == []
    assert mb.search_release("Herbie Hancock", "") == []
    assert mb.search_release("  ", "  ") == []
    assert called == []


def test_lookup_release_positions_are_continuous_across_media(monkeypatch):
    monkeypatch.setattr(mb, "_get", lambda path, params: _LOOKUP_RESPONSE)
    details = mb.lookup_release("rel-mbid-1")
    assert details["mbid"] == "rel-mbid-1"
    assert details["release_type"] == "Album"
    positions = [(t["position"], t["title"]) for t in details["tracks"]]
    assert positions == [
        (1, "Chameleon"), (2, "Watermelon Man"),
        (3, "Sly"), (4, "Vein Melter"),
    ]


# ── apply_to_recording() -- fill-if-null only ───────────────────────────────

def _make_studio_recording(mixed_titles=True):
    artist = _db.session.query(Artist).filter_by(name="Bill Evans").first()
    perf = Performance(artist_id=artist.id, start_year=1973, start_month=None,
                       start_day=None)
    _db.session.add(perf)
    _db.session.flush()
    rec = Recording(performance_id=perf.id, folder_path="Bill Evans/HeadHunters",
                    title="Head Hunters",
                    is_complete=True, is_official=False, kind="studio")
    _db.session.add(rec)
    _db.session.flush()
    titles = ["Chameleon Live", "", "", ""] if mixed_titles else ["", "", "", ""]
    for i, title in enumerate(titles, start=1):
        _db.session.add(Track(recording_id=rec.id, track_number=i, title=title,
                              duration=100, file_path="%02d.flac" % i))
    _db.session.commit()
    return rec, perf


def test_apply_to_recording_fills_only_null_fields_and_writes_event(app, seeded_ids):
    rec, perf = _make_studio_recording()

    release = {
        "mbid": "rel-mbid-1", "release_group_id": "rg-1", "release_type": "Album",
        "label": "Columbia", "catalog_number": "KC 32731", "country": "US",
        "date": "1973-10-13",
        "tracks": [
            {"position": 1, "title": "Chameleon"},
            {"position": 2, "title": "Watermelon Man"},
            {"position": 3, "title": "Sly"},
            {"position": 4, "title": "Vein Melter"},
        ],
    }

    mb.apply_to_recording(rec, release, status="matched")
    _db.session.commit()

    # Year was already set -- untouched. Month/day were null -- filled.
    assert perf.start_year == 1973
    assert perf.start_month == 10
    assert perf.start_day == 13

    tracks = sorted(rec.tracks, key=lambda t: t.track_number)
    # Track 1 already had a title -- untouched, NOT overwritten by "Chameleon".
    assert tracks[0].title == "Chameleon Live"
    # Tracks 2-4 were empty -- filled by position.
    assert tracks[1].title == "Watermelon Man"
    assert tracks[2].title == "Sly"
    assert tracks[3].title == "Vein Melter"

    assert rec.mb_release_id == "rel-mbid-1"
    assert rec.mb_release_group_id == "rg-1"
    assert rec.mb_release_status == "matched"
    assert rec.mb_release_type == "Album"
    assert rec.mb_label == "Columbia"
    assert rec.mb_catalog_number == "KC 32731"
    assert rec.mb_release_country == "US"
    assert rec.mb_release_checked_at is not None

    events = (_db.session.query(RecordingEvent)
             .filter_by(recording_id=rec.id, event_type="mb_release_matched")
             .all())
    assert len(events) == 1
    note = events[0].note
    assert "start_month" in note
    assert "start_day" in note
    assert "start_year" not in note  # already set -- never listed as filled
    assert "track_2_title" in note
    assert "track_3_title" in note
    assert "track_4_title" in note
    assert "track_1_title" not in note


def test_apply_to_recording_track_titles_skipped_on_count_mismatch(app, seeded_ids):
    rec, _perf = _make_studio_recording(mixed_titles=False)
    # Release has only 3 tracks; recording has 4 -- position mapping cannot
    # be trusted, so titles must be left alone entirely.
    release = {
        "mbid": "rel-mbid-1", "release_group_id": "rg-1", "release_type": "Album",
        "label": "Columbia", "catalog_number": "KC 32731", "country": "US",
        "date": "1973",
        "tracks": [
            {"position": 1, "title": "A"},
            {"position": 2, "title": "B"},
            {"position": 3, "title": "C"},
        ],
    }
    mb.apply_to_recording(rec, release, status="matched")
    _db.session.commit()
    assert all(t.title == "" for t in rec.tracks)


# ── try_match_release() -- automatic pass ──────────────────────────────────

def test_live_recording_is_never_matched(app, seeded_ids):
    """kind == 'live' -- try_match_release() must return without calling
    search_release() at all."""
    from app.models.recording import Recording as _Recording
    rec = _db.session.get(_Recording, seeded_ids["recording_id"])
    assert rec.kind == "live"

    called = []
    original = mb.search_release
    def _spy(*a, **k):
        called.append((a, k))
        return original(*a, **k)
    import unittest.mock as _mock
    with _mock.patch.object(mb, "search_release", side_effect=_spy):
        assert mb.try_match_release(rec) is None
    assert called == []


def test_lookups_disabled_under_testing_is_a_noop_with_no_get_call(app, seeded_ids):
    """enabled() is False under TESTING with no monkeypatching required --
    try_match_release() must return None and must never reach _get()."""
    assert mb.enabled() is False

    rec, _perf = _make_studio_recording()
    calls = []
    original_get = mb._get
    def _spy(*a, **k):
        calls.append((a, k))
        return original_get(*a, **k)
    import unittest.mock as _mock
    with _mock.patch.object(mb, "_get", side_effect=_spy):
        result = mb.try_match_release(rec)
    assert result is None
    assert calls == []
    assert rec.mb_release_status is None


def test_try_match_release_matched_end_to_end(app, monkeypatch, seeded_ids):
    """With MusicBrainz forced on and _get() stubbed, the full automatic
    pass runs search -> gate -> lookup -> apply."""
    rec, _perf = _make_studio_recording()

    monkeypatch.setattr(mb, "enabled", lambda: True)

    def _fake_get(path, params):
        if path == "release/":
            return _SEARCH_RESPONSE
        return _LOOKUP_RESPONSE

    monkeypatch.setattr(mb, "_get", _fake_get)
    status = mb.try_match_release(rec)
    _db.session.commit()

    assert status == "matched"
    assert rec.mb_release_status == "matched"
    assert rec.mb_release_id == "rel-mbid-1"


def test_try_match_release_ambiguous_or_none_sets_status_only(app, monkeypatch, seeded_ids):
    rec, _perf = _make_studio_recording()
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "_get", lambda path, params: {"releases": []})

    status = mb.try_match_release(rec)
    _db.session.commit()

    assert status == "none"
    assert rec.mb_release_status == "none"
    assert rec.mb_release_id is None
    assert rec.mb_release_checked_at is not None


# ── Circuit breaker ──────────────────────────────────────────────────────

def test_circuit_breaker_trips_after_three_failures_and_short_circuits():
    mb.reset_breaker()
    assert not mb.tripped()
    for _ in range(mb._MAX_CONSECUTIVE_FAILURES):
        mb._failures[0] += 1
    assert mb.tripped()
    assert mb._get("release/", {"query": "anything"}) is None
    mb.reset_breaker()
    assert not mb.tripped()


# ── link_release() / unlink_release() -- human path ─────────────────────────

def test_link_release_sets_linked_status(app, monkeypatch, seeded_ids):
    rec, _perf = _make_studio_recording()
    monkeypatch.setattr(mb, "_get", lambda path, params: _LOOKUP_RESPONSE)

    details = mb.link_release(rec, "rel-mbid-1")
    _db.session.commit()

    assert details is not None
    assert rec.mb_release_status == "linked"
    assert rec.mb_release_id == "rel-mbid-1"


def test_link_release_returns_none_when_lookup_fails(app, monkeypatch, seeded_ids):
    rec, _perf = _make_studio_recording()
    monkeypatch.setattr(mb, "_get", lambda path, params: None)
    assert mb.link_release(rec, "bad-mbid") is None
    assert rec.mb_release_id is None


def test_unlink_release_clears_columns_marks_unlinked_and_keeps_titles(app, monkeypatch, seeded_ids):
    rec, perf = _make_studio_recording()
    monkeypatch.setattr(mb, "_get", lambda path, params: _LOOKUP_RESPONSE)
    mb.link_release(rec, "rel-mbid-1")
    _db.session.commit()

    filled_title = rec.tracks[1].title  # "Watermelon Man" -- was filled above
    filled_year = perf.start_year

    mb.unlink_release(rec)
    _db.session.commit()

    assert rec.mb_release_id is None
    assert rec.mb_release_group_id is None
    assert rec.mb_release_type is None
    assert rec.mb_label is None
    assert rec.mb_catalog_number is None
    assert rec.mb_release_country is None
    assert rec.mb_release_status == "unlinked"
    assert rec.mb_release_checked_at is not None

    # Filled dates/titles are the collector's data now -- never reverted.
    assert rec.tracks[1].title == filled_title
    assert perf.start_year == filled_year

    events = (_db.session.query(RecordingEvent)
             .filter_by(recording_id=rec.id, event_type="mb_release_unlinked")
             .all())
    assert len(events) == 1


def test_unlink_sets_status_so_followups_never_requeue_it(app, monkeypatch, seeded_ids):
    """mb_release_status = 'unlinked', not NULL -- enqueue_followups()'s
    'IS NULL' query must never pick this recording back up."""
    from app.api import ingest as ingest_api
    rec, _perf = _make_studio_recording()
    monkeypatch.setattr(mb, "_get", lambda path, params: _LOOKUP_RESPONSE)
    mb.link_release(rec, "rel-mbid-1")
    mb.unlink_release(rec)
    _db.session.commit()

    assert rec.mb_release_status == "unlinked"
    assert rec.mb_release_status is not None


# ── Migration -- columns added, idempotent ──────────────────────────────────

def _make_bare_recording_db(path):
    con = sqlite3.connect(str(path))
    con.executescript("CREATE TABLE recording (id INTEGER PRIMARY KEY);")
    con.execute("INSERT INTO recording (id) VALUES (1)")
    con.commit()
    con.close()


def test_migration_adds_mb_release_columns_idempotently(tmp_path):
    db_path = tmp_path / "studio.db"
    _make_bare_recording_db(db_path)
    script = REPO_ROOT / "scripts" / "migrate_studio_records.py"

    expected_columns = {
        "mb_release_id", "mb_release_group_id",
        "mb_release_status", "mb_release_type", "mb_label",
        "mb_catalog_number", "mb_release_country", "mb_release_checked_at",
    }

    r1 = subprocess.run([sys.executable, str(script), "--db", str(db_path), "--apply"],
                        capture_output=True, text=True)
    assert r1.returncode == 0, r1.stdout + r1.stderr

    con = sqlite3.connect(str(db_path))
    cols = {row[1] for row in con.execute("PRAGMA table_info('recording')")}
    assert expected_columns <= cols
    assert con.execute("SELECT count(*) FROM recording").fetchone()[0] == 1
    con.close()

    # Second run: no-op, nothing to add, still succeeds.
    r2 = subprocess.run([sys.executable, str(script), "--db", str(db_path), "--apply"],
                        capture_output=True, text=True)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "already present" in r2.stdout


# ── mb_release follow-up kind (app/api/ingest.py) ───────────────────────────

def test_handle_mb_release_skips_when_already_looked_up(app, monkeypatch, seeded_ids):
    from app.api import ingest as ingest_api
    rec, _perf = _make_studio_recording()
    rec.mb_release_status = "none"
    _db.session.commit()

    called = []
    monkeypatch.setattr(mb, "try_match_release", lambda r: called.append(r.id))
    ingest_api._handle_mb_release(rec.id)
    assert called == []


def test_handle_mb_release_calls_try_match_when_never_looked_up(app, monkeypatch, seeded_ids):
    from app.api import ingest as ingest_api
    rec, _perf = _make_studio_recording()
    assert rec.mb_release_status is None

    called = []
    monkeypatch.setattr(mb, "try_match_release", lambda r: called.append(r.id) or "none")
    ingest_api._handle_mb_release(rec.id)
    assert called == [rec.id]


def test_enqueue_followups_queues_mb_release_only_for_studio(app, seeded_ids):
    from app.api import ingest as ingest_api
    import queue as _queue

    ingest_api._LANES = {"audio": ingest_api._Lane(), "net": ingest_api._Lane()}
    ingest_api._QUEUED_KEYS = set()
    ingest_api._PENDING_BY_KIND = {"audio": 0, "mb_artist": 0, "mb_release": 0, "images": 0}
    orig_worker = ingest_api._ANALYSIS_STATE["worker"]
    ingest_api._ANALYSIS_STATE["worker"] = True
    try:
        # Neutralize the live seeded recording/artist so counts are just this test's.
        from app.models.quality import RecordingQuality
        _db.session.add(RecordingQuality(recording_id=seeded_ids["recording_id"],
                                         listening_quality=80.0))
        artist = _db.session.get(Artist, seeded_ids["artist_id"])
        artist.mb_status = "matched"
        _db.session.commit()

        studio_rec, _perf = _make_studio_recording()
        counts = ingest_api.enqueue_followups()
        assert counts["mb_release"] == 1
        assert ("mb_release", studio_rec.id) in ingest_api._QUEUED_KEYS
    finally:
        ingest_api._ANALYSIS_STATE["worker"] = orig_worker
