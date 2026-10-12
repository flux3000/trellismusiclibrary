"""
tests/test_lomax_about_skills.py -- the musician and event skills, the personal reply on every
skill, and the event.history schema upgrade. No network (the fake client from test_lomax_core).
"""
import os
import sqlite3

import pytest
from sqlalchemy import create_engine

import app.lomax as lomax
from app.extensions import db
from app.models.artist import Artist
from app.models.event import Event
from app.models.musician import Musician
from app.utils.schema_upgrades import ensure_event_history

from tests.test_lomax_core import fake, run_skill  # noqa: F401

pytestmark = pytest.mark.usefixtures("fake")


@pytest.fixture
def eid(app):
    e = Event(name="Winter Series", country="US", start_year=1980)
    db.session.add(e)
    db.session.commit()
    return e.id


def test_musician_biography_auto_applies_and_restores(fake, seeded_ids):
    m = db.session.get(Musician, seeded_ids["musician_id"])
    m.bio = "Old."
    db.session.commit()
    fake.canned["submit_musician_biography"] = {"thinking": "t", "biography": "He played with Miles Davis.",
                                                "reply": "I wrote up his career."}
    run = run_skill("musician", "musician", m.id)
    assert db.session.get(Musician, m.id).bio == "He played with Miles Davis."
    got = lomax.get_run(run.id)["result"]
    assert got["replaced"] == {"field": "bio", "text": "Old.", "written": "He played with Miles Davis."}
    assert got["reply"] == "I wrote up his career."
    assert got["proposals"] == []
    lomax.restore(run.id)
    assert db.session.get(Musician, m.id).bio == "Old."


def test_musician_run_creates_no_artist_or_membership_rows(fake, seeded_ids):
    from app.models.musician import Membership
    n_art, n_mem = db.session.query(Artist).count(), db.session.query(Membership).count()
    fake.canned["submit_musician_biography"] = {"thinking": "t", "biography": "Played in The Band.",
                                                "members": [{"name": "X"}], "proposals": [{"field": "member"}]}
    run_skill("musician", "musician", seeded_ids["musician_id"])
    assert (db.session.query(Artist).count(), db.session.query(Membership).count()) == (n_art, n_mem)


def test_event_history_auto_applies_and_restores(fake, eid):
    db.session.get(Event, eid).history = "Old."
    db.session.commit()
    fake.canned["submit_event_history"] = {"thinking": "t", "history": "Ran in 1980.", "proposals": []}
    run = run_skill("event", "event", eid)
    assert db.session.get(Event, eid).history == "Ran in 1980."
    got = lomax.get_run(run.id)["result"]
    assert got["replaced"] == {"field": "history", "text": "Old.", "written": "Ran in 1980."}
    lomax.restore(run.id)
    assert db.session.get(Event, eid).history == "Old."


def test_event_proposals_apply_and_unknown_fields_drop(fake, eid):
    fake.canned["submit_event_history"] = {
        "thinking": "t", "history": "h",
        "proposals": [{"field": "start_date", "proposed": "1980-02-22", "confidence": "high", "source": "web"},
                      {"field": "end_date", "proposed": "not a date", "confidence": "low", "source": "web"},
                      {"field": "state", "proposed": "Tennessee", "confidence": "medium", "source": "web"},
                      {"field": "venue", "proposed": "x", "confidence": "low", "source": "web"}]}
    run = run_skill("event", "event", eid)
    props = lomax.get_run(run.id)["result"]["proposals"]
    assert [p["field"] for p in props] == ["start_date", "state"]
    assert props[1]["proposed"] == "TN"
    for p in props:
        lomax.decide(p["id"], "accepted")
    e = db.session.get(Event, eid)
    assert (e.start_year, e.start_month, e.start_day, e.state) == (1980, 2, 22, "TN")


def test_event_lineup_is_reference_data_flagged_when_in_library(fake, eid):
    fake.canned["submit_event_history"] = {"thinking": "t", "history": "h", "proposals": [],
                                           "lineup": ["Bill Evans", "Nobody Known", "bill evans"]}
    run = run_skill("event", "event", eid)
    got = lomax.get_run(run.id)["result"]
    assert got["lineup"] == [{"name": "Bill Evans", "in_library": True},
                             {"name": "Nobody Known", "in_library": False}]
    assert not any(p["field"] == "lineup" for p in got["proposals"])


def test_every_skill_schema_carries_a_reply_and_the_result_keeps_it():
    from app.lomax.skills import SKILLS
    for key, sk in SKILLS.items():
        assert "reply" in sk.submit_tool["input_schema"]["properties"], key


def test_a_reply_is_stored_without_dashes(fake, seeded_ids):
    fake.canned["submit_artist_history"] = {"thinking": "t", "biography": "b", "members": [],
                                            "reply": "I found a roster — and links."}
    run = run_skill("artist", "artist", seeded_ids["artist_id"])
    assert "—" not in lomax.get_run(run.id)["result"]["reply"]


def test_collector_opinion_rule_is_in_the_base_prompt():
    from app.lomax.prompts import BASE
    assert "only opinions found in sources" in BASE


def test_ensure_event_history_is_idempotent_and_never_creates_a_file(tmp_path):
    missing = tmp_path / "nope.db"
    assert ensure_event_history(create_engine("sqlite:///%s" % missing)) is None
    assert not missing.exists()
    path = tmp_path / "e.db"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE event (id INTEGER PRIMARY KEY, name TEXT)")
    con.commit()
    con.close()
    eng = create_engine("sqlite:///%s" % path)
    assert ensure_event_history(eng) is True
    assert ensure_event_history(eng) is None
    assert os.path.exists(str(path))


def test_a_decision_on_a_row_marked_agrees_is_recorded(app, seeded_ids):
    """The page offers Accept/Dismiss on an "agrees" row once the live value differs from the proposal."""
    from app.models.lomax import LomaxProposal
    run = lomax.create_run("recording", "recording", subject_id=seeded_ids["recording_id"], question="q")
    prop = LomaxProposal(run_id=run.id, field="lineage", proposed="SBD > DAT", agrees=True)
    db.session.add(prop)
    db.session.commit()
    got = lomax.decide(prop.id, "rejected")
    assert got["decision"] == "rejected" and got["agrees"] is False
