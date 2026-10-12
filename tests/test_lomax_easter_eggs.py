"""
tests/test_lomax_easter_eggs.py -- the Lomax easter-egg acknowledgments: chosen when a person starts a
run, never twice for one person, never back to back, never on an import run. No network, no worker.
"""
import os
import sqlite3

import pytest
from sqlalchemy import create_engine

import app.lomax as lomax
from app.extensions import db
from app.lomax import eggs
from app.models.lomax import LomaxEasterEggSeen
from app.models.user import User
from app.utils.schema_upgrades import ensure_lomax_easter_eggs


@pytest.fixture
def on(app):
    app.config.update(LOMAX_EGGS=True, LOMAX_EGG_ODDS=1)      # odds 1: every eligible run draws
    yield
    app.config.update(LOMAX_EGGS=False)


def _user():
    return User.query.first().id


def _ask(rid, uid, question="Tell me more"):
    return lomax.create_run("recording", "recording", subject_id=rid, question=question, user_id=uid)


def test_off_by_default_under_test(app, seeded_ids):
    assert _ask(seeded_ids["recording_id"], _user()).input_json is None


def test_each_egg_once_never_consecutive_then_exhausted(app, on, seeded_ids):
    rid, uid = seeded_ids["recording_id"], _user()
    shown, last_had_egg = [], False
    for _ in range(40):
        r = _ask(rid, uid)
        key = (lomax.run_dict(r)["ack_text"] and lomax._parsed(r.input_json)["egg"]) or None
        if key:
            assert not last_had_egg
            shown.append(key)
        last_had_egg = bool(key)
    assert len(shown) == len(set(shown)) == len(eggs.EGGS)
    assert db.session.query(LomaxEasterEggSeen).filter_by(user_id=uid).count() == len(eggs.EGGS)
    assert lomax.run_dict(_ask(rid, uid))["ack_text"] is None      # exhausted: normal acks only


def test_ack_text_is_the_egg_text_and_stable(app, on, seeded_ids):
    r = _ask(seeded_ids["recording_id"], _user())
    d = lomax.run_dict(r)
    assert d["ack_text"] in eggs.EGGS.values()
    assert lomax.run_dict(r)["ack_text"] == d["ack_text"]


def test_import_runs_never_get_one(app, on, seeded_ids):
    uid = _user()
    assert _ask(seeded_ids["recording_id"], uid, question=None).input_json is None
    assert _ask(seeded_ids["recording_id"], uid, question="  ").input_json is None
    assert db.session.query(LomaxEasterEggSeen).count() == 0


def test_a_folder_run_with_a_question_never_gets_one(app, on):
    for _ in range(10):
        r = lomax.create_run("recording", "folder", subject_key="/x/y", question="Who played?", user_id=_user(),
                             current={"info_file_content": "hi"})
        assert lomax.run_dict(r)["ack_text"] is None
        assert "egg" not in (r.input_json or "")
    assert db.session.query(LomaxEasterEggSeen).count() == 0


def test_ensure_lomax_easter_eggs_is_idempotent_and_never_creates_a_file(tmp_path):
    missing = tmp_path / "nope.db"
    assert ensure_lomax_easter_eggs(create_engine("sqlite:///%s" % missing)) is None
    assert not missing.exists()
    path = tmp_path / "e.db"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE user (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    eng = create_engine("sqlite:///%s" % path)
    assert ensure_lomax_easter_eggs(eng) is True
    assert ensure_lomax_easter_eggs(eng) is None
    assert os.path.exists(str(path))
