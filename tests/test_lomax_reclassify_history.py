"""
tests/test_lomax_reclassify_history.py -- a recording reclassified from live to Album (or
back) keeps its Lomax chat history and the recap handed to the next run (Ryan, 2026-10-15).
"""

import json

from app.extensions import db
from app.lomax import core, list_runs, prompts
from app.models.lomax import LomaxRun

KEY = "/tmp/queue/An Evening With Herbie Hancock & Chick Corea"
NOTE = "Content matches the 1978 album An Evening with Herbie Hancock & Chick Corea: In Concert."


def _run(skill, **kw):
    r = LomaxRun(skill=skill, subject_type="folder", subject_key=KEY, status=kw.pop("status", "done"),
                 result_json=json.dumps(kw.pop("result", {})), **kw)
    db.session.add(r)
    db.session.commit()
    return r


def test_history_spans_live_and_album(app):
    live = _run("recording", result={"provenance_notes": [NOTE]})
    album = _run("album", status="queued")
    ids = [r["id"] for r in list_runs("album", "folder", subject_key=KEY)]
    assert ids == [live.id, album.id]
    assert [r["id"] for r in list_runs("recording", "folder", subject_key=KEY)] == ids


def test_recap_carries_notes_and_the_reclassification(app):
    _run("recording", result={"provenance_notes": [NOTE]})
    album = _run("album", status="queued")
    prior = core.prior_for(album)
    assert prior and prior["provenance_notes"] == [NOTE]
    text = prompts.recap_text(prior)
    assert NOTE in text and "classified it as an album" in text


def test_other_skills_keep_their_own_history(app):
    _run("recording")
    assert list_runs("artist", "folder", subject_key=KEY) == []
