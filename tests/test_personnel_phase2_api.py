"""
tests/test_personnel_phase2_api.py — Per-Show Personnel, Phase 2 API surface
(2026-07-18): stint CRUD endpoints, Artist.default_personnel_mode, the
manual Performance.personnel_mode toggle (snapshot-on-explicit,
clear-on-inherit), and per-row instrument/note editing. These back the
Artist-page stint editor and the recording-page personnel pill row.
"""

import pytest

from app.extensions import db as _db
from app.models.musician import Membership
from app.models.performance_personnel import PerformancePersonnel


@pytest.fixture()
def api(app):
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


def test_get_artist_dedupes_multi_stint_member(api, seeded_ids):
    """The Artist.musicians dedupe fix: a person with 2 stints must appear
    once in `members`, carrying both stint rows, not twice."""
    artist_id = seeded_ids["artist_id"]
    musician_id = seeded_ids["musician_id"]

    r = api.post(f"/api/artists/{artist_id}/members/{musician_id}/stints",
                json={"start_year": 1990})
    assert r.status_code == 201

    r = api.get(f"/api/artists/{artist_id}")
    body = r.get_json()
    matches = [m for m in body["members"] if m["id"] == musician_id]
    assert len(matches) == 1
    assert len(matches[0]["stints"]) == 2


def test_add_edit_delete_stint(api, seeded_ids):
    artist_id = seeded_ids["artist_id"]
    musician_id = seeded_ids["musician_id"]

    r = api.post(f"/api/artists/{artist_id}/members/{musician_id}/stints",
                json={"start_year": 2001, "end_year": 2005})
    stint_id = r.get_json()["id"]

    r = api.put(f"/api/artists/stints/{stint_id}", json={"start_year": 2002})
    assert r.status_code == 200
    m = _db.session.get(Membership, stint_id)
    assert m.start_year == 2002 and m.end_year is None   # only sent fields applied, per the endpoint contract

    r = api.delete(f"/api/artists/stints/{stint_id}")
    assert r.status_code == 200
    assert _db.session.get(Membership, stint_id) is None


def test_delete_last_stint_refused(api, seeded_ids):
    """Deleting a member's ONLY stint is refused — drop them from the roster
    instead, which goes through the safe orphan-check path."""
    musician_id = seeded_ids["musician_id"]
    only_stint = _db.session.query(Membership).filter_by(musician_id=musician_id).first()

    r = api.delete(f"/api/artists/stints/{only_stint.id}")
    assert r.status_code == 409
    assert _db.session.get(Membership, only_stint.id) is not None


def test_default_personnel_mode_validated_and_persisted(api, seeded_ids):
    artist_id = seeded_ids["artist_id"]

    bad = api.put(f"/api/artists/{artist_id}", json={"default_personnel_mode": "sometimes"})
    assert bad.status_code == 400

    ok = api.put(f"/api/artists/{artist_id}", json={"default_personnel_mode": "explicit"})
    assert ok.status_code == 200

    r = api.get(f"/api/artists/{artist_id}")
    assert r.get_json()["default_personnel_mode"] == "explicit"


def test_new_performance_inherits_act_default_mode(api, seeded_ids):
    artist_id = seeded_ids["artist_id"]
    api.put(f"/api/artists/{artist_id}", json={"default_personnel_mode": "explicit"})

    r = api.post("/api/performances/", json={"artist_id": artist_id, "start_year": 1999})
    perf_id = r.get_json()["id"]

    body = api.get(f"/api/performances/{perf_id}").get_json()
    assert body["personnel_mode"] == "explicit"
    assert body["members"] == []   # explicit with no rows yet — nothing inherited


def test_manual_toggle_to_explicit_snapshots_then_back_clears(api, seeded_ids):
    perf_id = seeded_ids["performance_id"]

    r = api.put(f"/api/performances/{perf_id}", json={"personnel_mode": "explicit"})
    assert r.status_code == 200
    body = api.get(f"/api/performances/{perf_id}").get_json()
    assert body["personnel_mode"] == "explicit"
    assert [m["name"] for m in body["members"]] == ["Bill Evans"]   # snapshotted, nothing vanished
    assert _db.session.query(PerformancePersonnel).filter_by(performance_id=perf_id).count() == 1

    r = api.put(f"/api/performances/{perf_id}", json={"personnel_mode": "inherit"})
    assert r.status_code == 200
    body = api.get(f"/api/performances/{perf_id}").get_json()
    assert body["personnel_mode"] == "inherit"
    assert [m["name"] for m in body["members"]] == ["Bill Evans"]   # reverted cleanly to the act roster
    assert _db.session.query(PerformancePersonnel).filter_by(performance_id=perf_id).count() == 0


def test_invalid_personnel_mode_rejected(api, seeded_ids):
    r = api.put(f"/api/performances/{seeded_ids['performance_id']}",
               json={"personnel_mode": "sometimes"})
    assert r.status_code == 400


def test_update_personnel_row_instrument_and_note(api, seeded_ids):
    perf_id = seeded_ids["performance_id"]
    api.put(f"/api/performances/{perf_id}", json={"personnel_mode": "explicit"})
    body = api.get(f"/api/performances/{perf_id}").get_json()
    row_id = body["personnel"][0]["id"]
    assert row_id is not None

    r = api.put(f"/api/performances/{perf_id}/personnel/{row_id}",
               json={"instrument": "piano", "note": "solo set"})
    assert r.status_code == 200

    body = api.get(f"/api/performances/{perf_id}").get_json()
    row = body["personnel"][0]
    assert row["instrument"] == "piano"
    assert row["note"] == "solo set"


def test_update_personnel_row_wrong_performance_404s(api, seeded_ids):
    perf_id = seeded_ids["performance_id"]
    api.put(f"/api/performances/{perf_id}", json={"personnel_mode": "explicit"})
    row_id = api.get(f"/api/performances/{perf_id}").get_json()["personnel"][0]["id"]

    r = api.put(f"/api/performances/999999/personnel/{row_id}", json={"instrument": "sax"})
    assert r.status_code == 404
