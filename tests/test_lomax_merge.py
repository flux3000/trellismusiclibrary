"""
tests/test_lomax_merge.py -- the merged Recording skill (agreements, track proposals and their
apply), the study level and its migration, legacy "resolution" rows, and sources_plain.
No network (the fake client from test_lomax_core; runs finish inline).
"""
import json

import pytest
from sqlalchemy import text

import app.lomax as lomax
from app.extensions import db
from app.lomax import core
from app.models.lomax import LomaxProposal, LomaxRun
from app.models.recording import Recording
from app.models.recording_event import RecordingEvent
from app.models.track import Track
from app.models.user import User
from app.utils.schema_upgrades import ensure_lomax
from app.utils.sources_plain import sources_plain_field, with_sources_plain

from tests.test_lomax_api import api  # noqa: F401
from tests.test_lomax_core import fake, run_skill  # noqa: F401

pytestmark = pytest.mark.usefixtures("fake")


def _reply(**kw):
    return dict({"thinking": "t", "proposals": [], "tracks": []}, **kw)


def _run(fake, rid, **reply):
    fake.canned["submit_recording_research"] = _reply(**reply)
    return run_skill("recording", "recording", rid)


# ── agreements ───────────────────────────────────────────────────────────────

def test_agreeing_proposals_are_stored_flagged_and_cannot_be_decided(api, fake, seeded_ids):
    rid = seeded_ids["recording_id"]
    run = _run(fake, rid, proposals=[
        {"field": "artist", "proposed": "Bill Evans", "confidence": "high", "source": "db_match", "agrees": True},
        {"field": "source", "proposed": "audience", "confidence": "high", "source": "web"},    # equals the filed AUD? no
        {"field": "lineage", "proposed": "DAT > FLAC", "confidence": "medium", "source": "web"}])
    props = {p.field: p for p in db.session.get(LomaxRun, run.id).proposals}
    assert props["artist"].agrees is True and props["lineage"].agrees is False
    assert props["artist"].challenge is False
    r = api.post("/api/lomax/proposals/%d" % props["artist"].id, json={"decision": "accepted"})
    assert r.status_code >= 400 and db.session.get(LomaxProposal, props["artist"].id).decision is None
    got = {p["field"]: p for p in lomax.get_run(run.id)["result"]["proposals"]}
    assert got["artist"]["agrees"] is True


def test_a_value_equal_to_the_filed_one_counts_as_agreeing_without_the_flag(fake, seeded_ids):
    run = _run(fake, seeded_ids["recording_id"], proposals=[
        {"field": "artist", "proposed": "bill  evans", "confidence": "high", "source": "db_match"}])
    assert db.session.query(LomaxProposal).filter_by(run_id=run.id).one().agrees is True


def test_agreements_stay_out_of_the_recap_and_the_usage_counts(api, fake, seeded_ids):
    rid = seeded_ids["recording_id"]
    run = _run(fake, rid, proposals=[
        {"field": "artist", "proposed": "Bill Evans", "confidence": "high", "source": "db_match", "agrees": True},
        {"field": "lineage", "proposed": "DAT > FLAC", "confidence": "medium", "source": "web"}])
    nxt = LomaxRun(skill="recording", subject_type="recording", subject_id=rid, status="queued", level="study")
    db.session.add(nxt)
    db.session.commit()
    prior = core.prior_for(nxt)
    assert [p["field"] for p in prior["proposals"]] == ["lineage"]
    row = next(r for r in api.get("/api/lomax/usage").get_json()["runs"] if r["id"] == run.id)
    assert row["proposals"] == 1
    assert api.get("/api/lomax/usage").get_json()["totals"]["proposals"] == 1


def test_a_run_of_only_agreements_leaves_an_empty_recap(fake, seeded_ids):
    rid = seeded_ids["recording_id"]
    _run(fake, rid, proposals=[{"field": "artist", "proposed": "Bill Evans", "confidence": "high",
                                "source": "db_match", "agrees": True}])
    nxt = LomaxRun(skill="recording", subject_type="recording", subject_id=rid, status="queued", level="study")
    db.session.add(nxt)
    db.session.commit()
    assert core.prior_for(nxt)["proposals"] == []


# ── tracks ───────────────────────────────────────────────────────────────────

def test_tracks_become_proposals_against_the_current_track_values(fake, seeded_ids):
    rid = seeded_ids["recording_id"]
    first = db.session.query(Track).filter_by(recording_id=rid, track_number=1).one()
    run = _run(fake, rid, tracks=[
        {"number": 1, "title": first.title, "songwriter": "Bill Evans", "note": "Segue"},
        {"number": 2, "title": "Nardis", "songwriter": "", "note": ""},
        {"number": "x", "title": "bad"}])
    got = {p["field"]: p for p in lomax.get_run(run.id)["result"]["proposals"]}
    assert got["track.1.title"]["agrees"] is True
    assert got["track.1.songwriter"]["agrees"] is False and not got["track.1.songwriter"]["current"]
    assert "track.1.note" not in got                    # no info file footnote supports it: dropped
    assert got["track.2.title"]["proposed"] == "Nardis" and got["track.2.title"]["agrees"] is False
    assert "track.2.songwriter" not in got and "track.2.note" not in got
    result = lomax.get_run(run.id)["result"]
    assert [t["number"] for t in result["tracks"]] == [1, 2]
    assert {"number": 2, "title": "Nardis"} in result["track_titles"]


def test_accepting_track_proposals_writes_the_track_and_logs_it(api, fake, seeded_ids):
    rid = seeded_ids["recording_id"]
    db.session.get(Recording, rid).info_file_content = "2. Nardis *\n* Long intro with Miles Davis on trumpet"
    db.session.commit()
    run = _run(fake, rid, tracks=[{"number": 2, "title": "Nardis", "songwriter": "Miles Davis",
                                   "note": "Long intro with Miles Davis on trumpet"}])
    props = {p.field: p for p in db.session.get(LomaxRun, run.id).proposals}
    for f in ("track.2.title", "track.2.songwriter", "track.2.note"):
        r = api.post("/api/lomax/proposals/%d" % props[f].id, json={"decision": "accepted"})
        assert r.status_code == 200, r.get_json()
    t = db.session.query(Track).filter_by(recording_id=rid, track_number=2).one()
    assert (t.title, t.songwriter, t.notes) == ("Nardis", "Miles Davis", "Long intro with Miles Davis on trumpet")
    notes = [e.note for e in db.session.query(RecordingEvent).filter_by(recording_id=rid)]
    assert any("Lomax proposal accepted: track.2.songwriter" in (n or "") for n in notes)


def test_a_track_proposal_for_a_missing_track_is_refused(api, fake, seeded_ids):
    rid = seeded_ids["recording_id"]
    run = _run(fake, rid, tracks=[{"number": 99, "title": "Ghost"}])
    p = db.session.get(LomaxRun, run.id).proposals[0]
    r = api.post("/api/lomax/proposals/%d" % p.id, json={"decision": "accepted"})
    assert r.status_code >= 400 and db.session.get(LomaxProposal, p.id).decision is None


def test_the_instructions_ask_for_every_field_and_every_track():
    from app.lomax.skills import SKILLS
    ins = SKILLS["recording"].instructions
    assert "agrees=true" in ins and "EVERY track" in ins and "Segue notation" in ins
    assert sorted(SKILLS) == ["album", "artist", "recording", "venue"]
    assert "—" not in ins


# ── study level ──────────────────────────────────────────────────────────────

def test_study_is_the_level_and_read_is_accepted_as_an_alias(api, fake, seeded_ids):
    assert core.LEVELS == ("study", "research")
    for level in ("read", "study"):
        r = api.post("/api/lomax/runs", json={"skill": "recording", "subject_type": "recording",
                                              "subject_id": seeded_ids["recording_id"], "level": level})
        assert r.status_code in (200, 201, 202), r.get_json()
        assert r.get_json()["level"] == "study"
    bad = api.post("/api/lomax/runs", json={"skill": "recording", "subject_type": "recording",
                                            "subject_id": seeded_ids["recording_id"], "level": "deep"})
    assert bad.status_code == 400 and "study or research" in bad.get_json()["error"]
    est = api.get("/api/lomax/estimate?skill=recording&level=read").get_json()
    assert est["max_searches"] == 0


def test_the_migration_renames_levels_and_skills_and_is_idempotent(app, seeded_ids):
    rid = seeded_ids["recording_id"]
    for skill, level in (("resolution", "read"), ("recording", "read"), ("artist", "research")):
        db.session.add(LomaxRun(skill=skill, subject_type="recording", subject_id=rid, status="done", level=level))
    db.session.commit()
    ensure_lomax(db.engine)
    ensure_lomax(db.engine)
    db.session.expire_all()
    rows = sorted((r.skill, r.level) for r in db.session.query(LomaxRun))
    assert rows == [("artist", "research"), ("recording", "study"), ("recording", "study")]


def test_a_legacy_resolution_row_still_reads_as_a_recording_run(app, api, seeded_ids):
    rid = seeded_ids["recording_id"]
    db.session.add(LomaxRun(skill="resolution", subject_type="recording", subject_id=rid, status="done",
                            level="read", result_json=json.dumps({"thinking": "old", "proposals": []})))
    db.session.commit()
    run = db.session.query(LomaxRun).filter_by(skill="resolution").one()
    got = lomax.get_run(run.id)
    assert got["skill"] == "recording" and got["level"] == "study"
    assert lomax.latest_result("recording", rid, ("recording", "resolution"))["thinking"] == "old"
    row = next(r for r in api.get("/api/lomax/usage").get_json()["runs"] if r["id"] == run.id)
    assert row["skill"] == "recording" and row["skill_label"] == "Recording" and row["level"] == "study"


# ── sources_plain ────────────────────────────────────────────────────────────

def test_sources_plain_maps_each_source_to_its_row():
    field = {"value": "Sprague Hall", "candidates": {"tags": "Sprague", "info": "Sprague hall", "folder": "Sprague Memorial"},
             "evidence": [{"source": "info", "text": "at Sprague hall tonight", "line": 2},
                          {"source": "atlas", "text": "Sprague Memorial Hall", "line": None}]}
    assert sources_plain_field(field) == {"metadata": "Sprague", "info_files": "Sprague hall",
                                          "folder": "Sprague Memorial", "reference_match": "Sprague Memorial Hall"}


def test_sources_plain_falls_back_to_evidence_text_and_uses_none_for_empty_sources():
    field = {"candidates": {}, "evidence": [{"source": "folder", "text": "1980-02-22"},
                                            {"source": "library", "text": "  "}]}
    assert sources_plain_field(field) == {"metadata": None, "info_files": None, "folder": "1980-02-22",
                                          "reference_match": None}
    assert sources_plain_field(None) == {"metadata": None, "info_files": None, "folder": None, "reference_match": None}


def test_with_sources_plain_adds_one_key_and_leaves_the_rest():
    resolved = {"date": {"candidates": {"tags": "1980"}}, "venue": {"candidates": {}}, "kind": "show", "tracks": []}
    out = with_sources_plain(resolved)
    assert out is resolved and out["kind"] == "show"
    assert set(out["sources_plain"]) == {"date", "venue"} and out["sources_plain"]["date"]["metadata"] == "1980"
    assert with_sources_plain(None) is None


def test_the_recording_payload_carries_sources_plain(api, seeded_ids):
    rec = db.session.get(Recording, seeded_ids["recording_id"])
    rec.resolver_json = json.dumps({"venue": {"value": "X", "candidates": {"info": "X hall"}, "evidence": []}})
    db.session.commit()
    page = api.get("/api/recordings/%d" % rec.id).get_json()
    assert page["resolver_json"]["sources_plain"]["venue"]["info_files"] == "X hall"


def test_the_scan_payload_carries_sources_plain(app, tmp_path):
    from app.utils.ingest import build_scan_payload
    folder = tmp_path / "Bill Evans 1980-02-22"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"")
    resp = build_scan_payload(str(folder))
    assert "sources_plain" in resp["resolved"]
