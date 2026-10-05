"""
tests/test_lomax_followup.py -- Venue History auto-apply and restore, the replaced text on both
auto-applies, and the usage log. No network (the fake client from test_lomax_core).
"""
import json

import pytest

import app.lomax as lomax
from app.extensions import db
from app.models.artist import Artist
from app.models.lomax import LomaxProposal, LomaxRun
from app.models.performance import Performance
from app.models.user import User
from app.models.venue import Venue
from app.utils.schema_upgrades import ensure_lomax, ensure_venue_history

from tests.test_lomax_core import fake, run_skill  # noqa: F401

pytestmark = pytest.mark.usefixtures("fake")


@pytest.fixture
def api(app):
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(User.query.first().id)
        s["_fresh"] = True
    return client


@pytest.fixture
def vid(app, seeded_ids):
    return db.session.get(Performance, seeded_ids["performance_id"]).venue_id


def test_venue_history_auto_applies_and_records_what_it_replaced(fake, vid):
    db.session.get(Venue, vid).history = "Old text."
    db.session.commit()
    fake.canned["submit_venue_history"] = {"thinking": "t", "history": "Opened 1921.", "proposals": []}
    run = run_skill("venue", "venue", vid)
    assert db.session.get(Venue, vid).history == "Opened 1921."
    got = lomax.get_run(run.id)["result"]
    assert got["replaced"] == {"field": "history", "text": "Old text.", "written": "Opened 1921."}
    assert got["applied"] == {"history": True}


def test_artist_auto_apply_records_the_replaced_biography(fake, seeded_ids):
    a = db.session.get(Artist, seeded_ids["artist_id"])
    a.bio = None
    db.session.commit()
    fake.canned["submit_artist_history"] = {"thinking": "t", "biography": "New bio.", "members": []}
    run = run_skill("artist", "artist", a.id)
    assert lomax.get_run(run.id)["result"]["replaced"] == {"field": "bio", "text": "", "written": "New bio."}


def test_an_empty_history_writes_nothing(fake, vid):
    run = run_skill("venue", "venue", vid)   # default canned history is "h"; blank it
    fake.canned["submit_venue_history"] = {"thinking": "t", "history": "", "proposals": []}
    db.session.get(Venue, vid).history = "Keep."
    db.session.commit()
    run_skill("venue", "venue", vid)
    assert db.session.get(Venue, vid).history == "Keep."


def test_restore_puts_the_replaced_text_back(api, fake, seeded_ids):
    a = db.session.get(Artist, seeded_ids["artist_id"])
    a.bio = "Mine."
    db.session.commit()
    fake.canned["submit_artist_history"] = {"thinking": "t", "biography": "Lomax's.", "members": []}
    run = run_skill("artist", "artist", a.id)
    assert db.session.get(Artist, a.id).bio == "Lomax's."
    r = api.post("/api/lomax/runs/%d/restore" % run.id)
    assert r.status_code == 200 and r.get_json()["result"]["restored_at"]
    assert db.session.get(Artist, a.id).bio == "Mine."
    assert api.post("/api/lomax/runs/%d/restore" % run.id).status_code == 400    # only once


def test_restore_after_a_manual_edit_is_409(api, fake, vid):
    db.session.get(Venue, vid).history = "Before."
    db.session.commit()
    fake.canned["submit_venue_history"] = {"thinking": "t", "history": "Lomax.", "proposals": []}
    run = run_skill("venue", "venue", vid)
    db.session.get(Venue, vid).history = "Lomax. Plus my edit."
    db.session.commit()
    r = api.post("/api/lomax/runs/%d/restore" % run.id)
    assert r.status_code == 409 and r.get_json() == {"error": "edited_since"}
    assert db.session.get(Venue, vid).history == "Lomax. Plus my edit."
    assert api.post("/api/lomax/runs/999/restore").status_code == 404


def test_venue_history_is_in_the_venue_payload(api, vid):
    db.session.get(Venue, vid).history = "Opened 1921."
    db.session.commit()
    assert api.get("/api/venues/%d" % vid).get_json()["history"] == "Opened 1921."


def test_ensure_venue_history_adds_the_column_once(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    path = tmp_path / "v.db"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE venue (id INTEGER PRIMARY KEY, name TEXT)")
    con.commit()
    con.close()
    eng = create_engine("sqlite:///%s" % path)
    assert ensure_venue_history(eng) is True
    assert ensure_venue_history(eng) is None


def test_ensure_lomax_adds_challenge_to_an_older_proposal_table(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    path = tmp_path / "o.db"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE artist (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE lomax_proposal (id INTEGER PRIMARY KEY, run_id INTEGER, field TEXT, proposed TEXT)")
    con.commit()
    con.close()
    eng = create_engine("sqlite:///%s" % path)
    ensure_lomax(eng)
    cols = {r[1] for r in sqlite3.connect(str(path)).execute("PRAGMA table_info(lomax_proposal)")}
    assert "challenge" in cols


# ── the usage log ────────────────────────────────────────────────────────────

def _usage(i, o, s):
    return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o, "web_search_requests": s}


@pytest.fixture
def runs(app, seeded_ids, vid):
    specs = [("recording", "recording", seeded_ids["recording_id"], _usage(1000, 100, 2)),
             ("artist", "artist", seeded_ids["artist_id"], _usage(2000, 200, 4)),
             ("venue", "venue", vid, _usage(500, 50, 0)),
             ("recording", "folder", None, None)]
    out = []
    for skill, st, sid, usage in specs:
        r = LomaxRun(skill=skill, subject_type=st, subject_id=sid, subject_key="/x/Some Folder" if st == "folder" else None,
                     status="done", level="study", usage_json=json.dumps(usage) if usage else None)
        db.session.add(r)
        out.append(r)
    db.session.commit()
    db.session.add(LomaxProposal(run_id=out[0].id, field="source", proposed="SBD", decision="accepted"))
    db.session.add(LomaxProposal(run_id=out[0].id, field="lineage", proposed="x", decision="rejected"))
    db.session.add(LomaxProposal(run_id=out[1].id, field="resource", proposed="{}"))
    db.session.commit()
    return out


def test_usage_totals_sum_across_all_runs(api, runs):
    got = api.get("/api/lomax/usage").get_json()
    t = got["totals"]
    assert (t["runs"], t["input_tokens"], t["output_tokens"], t["total_tokens"], t["web_searches"]) == (4, 3500, 350, 3850, 6)
    assert (t["proposals"], t["accepted"]) == (3, 1) and t["since"]
    assert got["total"] == 4
    assert not any("cent" in k or "cost" in k or "usd" in k for k in t)


def test_usage_rows_are_newest_first_with_user_facing_labels(api, runs, seeded_ids):
    rows = api.get("/api/lomax/usage").get_json()["runs"]
    assert [r["id"] for r in rows] == sorted((r.id for r in runs), reverse=True)
    by = {r["id"]: r for r in rows}
    rec = by[runs[0].id]
    assert rec["skill_label"] == "Recording"
    assert rec["subject_label"] == "Bill Evans · 1980-02-22 · Sprague Memorial Hall"
    assert (rec["proposals"], rec["accepted"], rec["total_tokens"], rec["web_searches"]) == (2, 1, 1100, 2)
    assert by[runs[1].id]["skill_label"] == "Artist History" and by[runs[1].id]["subject_label"] == "Bill Evans"
    assert by[runs[2].id]["skill_label"] == "Venue History" and by[runs[2].id]["subject_label"] == "Sprague Memorial Hall"
    assert by[runs[3].id]["subject_label"] == "Some Folder" and by[runs[3].id]["total_tokens"] == 0
    for key in ("created_at", "subject_type", "subject_id", "level", "status", "error", "question"):
        assert key in rec


def test_usage_paging(api, runs):
    first = api.get("/api/lomax/usage?limit=3&offset=0").get_json()
    second = api.get("/api/lomax/usage?limit=3&offset=3").get_json()
    assert len(first["runs"]) == 3 and len(second["runs"]) == 1 and first["total"] == 4
    assert first["totals"] == second["totals"]            # totals cover every run, not the page


def test_migrated_runs_are_counted(api, app, seeded_ids):
    from app.models.recording import Recording
    db.session.get(Recording, seeded_ids["recording_id"]).ai_research_json = json.dumps(
        {"thinking": "t", "usage": _usage(900, 100, 1)})
    db.session.commit()
    ensure_lomax(db.engine)
    t = api.get("/api/lomax/usage").get_json()["totals"]
    assert (t["runs"], t["total_tokens"], t["web_searches"]) == (1, 1000, 1)
