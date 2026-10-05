"""
tests/test_lomax_migration.py -- the one-time copy of recording.ai_research_json,
artist.dossier_json and artist.lineup_json into lomax_run, from the same upgrade path the app runs
at boot. Idempotent; the legacy columns are left in place and read by nothing.
"""
import json

import pytest

from app.extensions import db
from app.models.artist import Artist
from app.models.lomax import LomaxRun
from app.models.recording import Recording
from app.models.user import User
from app.utils.schema_upgrades import ensure_lomax

from tests.test_lomax_core import fake  # noqa: F401

pytestmark = pytest.mark.usefixtures("fake")

AI = {"thinking": "t", "model": "claude-x", "usage": {"total_tokens": 900},
      "proposals": [{"field": "date", "proposed": "1980-02-22", "confidence": "high", "source": "web"}],
      "track_titles": [{"number": 1, "title": "My Foolish Heart"}], "verify_items": [], "sources": []}
DOSSIER = {"thinking": "d", "biography": "A pianist.", "resources": [{"label": "L", "url": "https://x.example"}],
           "members": [], "sources": [], "mode": "bio"}
LINEUP = {"thinking": "l", "members": [{"name": "Scott LaFaro", "confidence": "high"}], "resources": [],
          "sources": [], "mode": "lineup"}


@pytest.fixture
def legacy(app, seeded_ids):
    rec = db.session.get(Recording, seeded_ids["recording_id"])
    art = db.session.get(Artist, seeded_ids["artist_id"])
    rec.ai_research_json, art.dossier_json, art.lineup_json = json.dumps(AI), json.dumps(DOSSIER), json.dumps(LINEUP)
    db.session.commit()
    return seeded_ids


def test_all_three_columns_are_copied_into_done_runs(legacy):
    assert ensure_lomax(db.engine)["copied"] == 3
    runs = {r.migrated_from: r for r in db.session.query(LomaxRun).all()}
    assert set(runs) == {"ai_research_json", "dossier_json", "lineup_json"}
    r = runs["ai_research_json"]
    assert (r.skill, r.subject_type, r.subject_id, r.status) == ("recording", "recording", legacy["recording_id"], "done")
    assert json.loads(r.result_json) == AI and r.model == "claude-x" and json.loads(r.usage_json) == AI["usage"]
    assert (runs["dossier_json"].skill, runs["dossier_json"].subject_id) == ("artist", legacy["artist_id"])
    assert json.loads(runs["lineup_json"].result_json)["members"][0]["name"] == "Scott LaFaro"


def test_running_it_again_copies_nothing_more(legacy):
    ensure_lomax(db.engine)
    again = ensure_lomax(db.engine)
    assert again["copied"] == 0
    assert db.session.query(LomaxRun).count() == 3


def test_the_legacy_columns_are_left_in_place(legacy):
    ensure_lomax(db.engine)
    assert db.session.get(Recording, legacy["recording_id"]).ai_research_json is not None


def test_a_blank_or_corrupt_blob_is_skipped_not_fatal(legacy):
    art = db.session.get(Artist, legacy["artist_id"])
    art.dossier_json, art.lineup_json = "{not json", ""
    db.session.commit()
    assert ensure_lomax(db.engine)["copied"] == 1


def test_pages_read_the_migrated_runs_in_the_payload_shape_they_always_had(app, legacy):
    ensure_lomax(db.engine)
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(User.query.first().id)
        s["_fresh"] = True
    art = client.get("/api/artists/%d" % legacy["artist_id"]).get_json()
    assert art["dossier"]["biography"] == "A pianist." and art["dossier"]["resources"][0]["url"] == "https://x.example"
    assert art["lineup"]["members"][0]["name"] == "Scott LaFaro"
    rec = client.get("/api/recordings/%d" % legacy["recording_id"]).get_json()
    assert rec["ai_research"]["track_titles"][0]["title"] == "My Foolish Heart"
    assert rec["ai_research"]["proposals"][0]["proposed"] == "1980-02-22"


def test_a_run_left_running_by_a_previous_process_becomes_interrupted(app, seeded_ids):
    db.session.add_all([LomaxRun(skill="recording", subject_type="recording", subject_id=seeded_ids["recording_id"],
                                 status=s) for s in ("running", "queued", "done")])
    db.session.commit()
    assert ensure_lomax(db.engine)["interrupted"] == 2
    states = sorted((r.status, r.error) for r in db.session.query(LomaxRun).all())
    assert states == [("done", None), ("error", "Interrupted"), ("error", "Interrupted")]


def test_it_leaves_a_database_with_no_file_alone(tmp_path):
    from sqlalchemy import create_engine
    eng = create_engine("sqlite:///%s" % (tmp_path / "nope" / "x.db"))
    assert ensure_lomax(eng) is None
    assert not (tmp_path / "nope").exists()


def test_it_creates_the_tables_on_a_database_that_lacks_them(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine, inspect
    path = tmp_path / "old.db"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE artist (id INTEGER PRIMARY KEY, name TEXT, dossier_json TEXT, lineup_json TEXT)")
    con.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, ai_research_json TEXT)")
    con.execute("INSERT INTO artist (id, name, dossier_json) VALUES (1, 'A', ?)", (json.dumps(DOSSIER),))
    con.commit()
    con.close()
    eng = create_engine("sqlite:///%s" % path)
    assert ensure_lomax(eng)["copied"] == 1
    assert {"lomax_run", "lomax_proposal"} <= set(inspect(eng).get_table_names())
    assert ensure_lomax(eng)["copied"] == 0
