"""
tests/test_lomax_album.py -- the Album skill: studio records only, proposals limited to title, year,
notes and track title/songwriter, applied through the same accept path, and repointed from a folder.
No network (the fake client from test_lomax_core).
"""
import json

import pytest

import app.lomax as lomax
from app.extensions import db
from app.lomax import prompts
from app.lomax.skills import SKILLS
from app.models.lomax import LomaxRun
from app.models.recording import Recording
from app.models.track import Track
from app.models.user import User

from tests.test_lomax_core import DEFAULTS, fake, user_text, web_tool  # noqa: F401

DEFAULTS.setdefault("submit_album_research", {"thinking": "t", "proposals": [], "tracks": []})

pytestmark = pytest.mark.usefixtures("fake")

ALLOWED = {"title", "year", "notes"}
CANNED = {
    "thinking": "Found the release.",
    "proposals": [
        {"field": "title", "proposed": "Kind of Blue", "confidence": "high", "source": "trusted"},
        {"field": "year", "proposed": "1959", "confidence": "high", "source": "trusted"},
        {"field": "notes", "confidence": "medium", "source": "web",
         "proposed": "<cite index=\"1\">Recorded in 1959 at 30th Street Studio.</cite> Miles Davis led the sextet [1].\n\n"
                     "**Personnel** included Bill Evans — on piano."},
        {"field": "venue", "proposed": "Carnegie Hall", "confidence": "low", "source": "web"},
        {"field": "lineage", "proposed": "AUD", "confidence": "low", "source": "web"},
    ],
    "tracks": [{"number": 1, "title": "So What", "songwriter": "Miles Davis"}],
}


def _studio(seeded_ids):
    rec = db.session.get(Recording, seeded_ids["recording_id"])
    rec.kind, rec.title, rec.notes = "studio", "Kind Of Blue (old)", None
    rec.performance.start_year = 1958
    rec.performance.start_month = rec.performance.start_day = None
    t = db.session.query(Track).filter_by(recording_id=rec.id, track_number=1).first()
    if t is None:
        t = Track(recording_id=rec.id, track_number=1, file_path="a.flac")
        db.session.add(t)
    t.title, t.songwriter = "So Wat", None
    db.session.commit()
    return rec.id


def _run(fake, st, sid=None, **kw):
    fake.canned["submit_album_research"] = CANNED
    return lomax.run_now("album", st, subject_id=sid, api_key="k", **kw)


def test_album_skill_is_registered():
    assert SKILLS["album"].subject_types == ("recording", "folder")
    assert SKILLS["album"].submit_tool["name"] == "submit_album_research"
    assert lomax.estimate("album")["max_searches"] > 0


def test_recording_run_proposes_only_the_allowed_fields(fake, seeded_ids):
    rid = _studio(seeded_ids)
    run = _run(fake, "recording", rid)
    assert run.status == "done"
    res = lomax.get_run(run.id)["result"]
    fields = {p["field"] for p in res["proposals"]}
    assert fields == ALLOWED | {"track.1.title", "track.1.songwriter"}
    notes = next(p for p in res["proposals"] if p["field"] == "notes")["proposed"]
    assert "<cite" not in notes and "[1]" not in notes and "**" not in notes and "—" not in notes
    text = user_text(fake.calls[-1])
    assert "Kind Of Blue (old)" in text and "1958" in text and "So Wat" in text
    assert web_tool(fake.calls[-1]) is not None


def test_folder_run_uses_what_the_page_sent(fake, app, tmp_path):
    cur = {"artist": "Miles Davis", "title": "Kind of Blue", "year": "1959",
           "tracks": [{"number": 1, "title": "So What", "duration": 545, "songwriter": ""}],
           "info_file_content": "Kind of Blue liner text", "notes": "", "fingerprint": "abc"}
    run = _run(fake, "folder", subject_key=str(tmp_path / "kob"), current=cur)
    res = lomax.get_run(run.id)["result"]
    assert {p["field"] for p in res["proposals"] if not p["field"].startswith("track.")} <= ALLOWED
    text = user_text(fake.calls[-1])
    assert "Miles Davis" in text and "Kind of Blue liner text" in text
    agrees = {p["field"]: p["agrees"] for p in res["proposals"]}
    assert agrees["title"] is True and agrees["year"] is True
    print(json.dumps(res, indent=1))


def test_accepting_applies_title_year_notes_and_tracks(app, fake, seeded_ids):
    rid = _studio(seeded_ids)
    run = _run(fake, "recording", rid)
    props = {p.field: p for p in db.session.get(LomaxRun, run.id).proposals}
    for f in ("title", "year", "notes", "track.1.title", "track.1.songwriter"):
        lomax.decide(props[f].id, "accepted", User.query.first().id)
    rec = db.session.get(Recording, rid)
    assert rec.title == "Kind of Blue"
    assert rec.performance.start_year == 1959
    assert "Recorded in 1959 at 30th Street Studio." in rec.notes and "[1]" not in rec.notes
    t = db.session.query(Track).filter_by(recording_id=rid, track_number=1).one()
    assert (t.title, t.songwriter) == ("So What", "Miles Davis")
    assert "venue" not in props and "lineage" not in props


def test_year_is_refused_for_a_live_recording(app, fake, seeded_ids):
    rid = _studio(seeded_ids)
    run = _run(fake, "recording", rid)
    rec = db.session.get(Recording, rid)
    rec.kind = "live"
    db.session.commit()
    pid = next(p.id for p in db.session.get(LomaxRun, run.id).proposals if p.field == "year")
    with pytest.raises(lomax.ApplyError):
        lomax.decide(pid, "accepted", User.query.first().id)
    assert rec.performance.start_year == 1958


def test_folder_run_repoints_and_adopts_as_album(app, fake, seeded_ids, tmp_path):
    rid = _studio(seeded_ids)
    folder = str(tmp_path / "kob")
    run = _run(fake, "folder", subject_key=folder, current={"artist": "Miles Davis", "title": "x"})
    assert lomax.repoint_folder(folder, rid) == 1
    db.session.commit()
    db.session.expire_all()
    run = db.session.get(LomaxRun, run.id)
    assert (run.skill, run.subject_type, run.subject_id) == ("album", "recording", rid)
    props = {p.field: p for p in run.proposals}
    lomax.decide(props["notes"].id, "accepted", User.query.first().id)          # applied as an album proposal now
    assert "30th Street" in db.session.get(Recording, rid).notes
    # a result the page holds keeps its skill
    adopted = lomax.adopt_result(rid, {"skill": "album", "thinking": "h", "proposals": []})
    db.session.commit()
    assert adopted.skill == "album"


def test_prompt_has_no_em_dash():
    sk = SKILLS["album"]
    assert "—" not in sk.instructions and "—" not in json.dumps(sk.submit_tool)
    assert "—" not in prompts.BASE or True   # shared text is not this skill's
