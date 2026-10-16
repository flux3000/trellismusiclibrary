"""
tests/test_lomax_official_release.py -- a recording run that finds the audio is an official release
proposes 'kind' (and the album fields); accepting 'kind' on a saved recording sets it to studio; an
album run on a record with no title identifies the release first. No network (fake client).
"""
import pytest

import app.lomax as lomax
from app.extensions import db
from app.lomax.skills import SKILLS
from app.models.recording import Recording
from app.models.user import User

from tests.test_lomax_core import DEFAULTS, fake, user_text  # noqa: F401

DEFAULTS.setdefault("submit_album_research", {"thinking": "t", "proposals": [], "tracks": []})
pytestmark = pytest.mark.usefixtures("fake")

OFFICIAL = {
    "thinking": "The track list and timings match the 1978 release.",
    "proposals": [
        {"field": "kind", "proposed": "album", "confidence": "high", "source": "trusted"},
        {"field": "title", "proposed": "An Evening with Herbie Hancock & Chick Corea: In Concert",
         "confidence": "high", "source": "trusted"},
        {"field": "year", "proposed": "Released 1978", "confidence": "high", "source": "trusted"},
        {"field": "artist", "proposed": "Herbie Hancock", "confidence": "medium", "source": "web"},
    ],
    "tracks": [],
}


def test_recording_schema_accepts_kind_and_album_fields():
    enum = SKILLS["recording"].submit_tool["input_schema"]["properties"]["proposals"]["items"]["properties"]["field"]["enum"]
    assert {"kind", "title", "year", "notes"} <= set(enum)
    assert "'kind'" in SKILLS["recording"].instructions


def test_recording_run_keeps_kind_and_album_fields(fake, seeded_ids):
    fake.canned["submit_recording_research"] = OFFICIAL
    run = lomax.run_now("recording", "recording", subject_id=seeded_ids["recording_id"], api_key="k")
    props = {p["field"]: p for p in lomax.get_run(run.id)["result"]["proposals"]}
    assert props["kind"]["proposed"] == "album" and props["kind"]["agrees"] is False
    assert props["year"]["proposed"] == "1978" and "Herbie" in props["title"]["proposed"]


def test_album_fields_dropped_without_kind(fake, seeded_ids):
    fake.canned["submit_recording_research"] = {"thinking": "t", "tracks": [], "proposals": [
        {"field": "title", "proposed": "X", "confidence": "low", "source": "web"},
        {"field": "year", "proposed": "1978", "confidence": "low", "source": "web"}]}
    run = lomax.run_now("recording", "recording", subject_id=seeded_ids["recording_id"], api_key="k")
    assert not lomax.get_run(run.id)["result"]["proposals"]


def test_accepting_kind_sets_studio_then_album_fields_apply(fake, seeded_ids):
    rid = seeded_ids["recording_id"]
    fake.canned["submit_recording_research"] = OFFICIAL
    run = lomax.run_now("recording", "recording", subject_id=rid, api_key="k")
    assert db.session.get(Recording, rid).kind != "studio"
    props = {p["field"]: p for p in lomax.get_run(run.id)["result"]["proposals"]}
    lomax.decide(props["kind"]["id"], "accepted", User.query.first().id)
    assert db.session.get(Recording, rid).kind == "studio"
    lomax.decide(props["year"]["id"], "accepted", User.query.first().id)
    assert db.session.get(Recording, rid).performance.start_year == 1978


def test_accepting_title_on_a_live_recording_classifies_first(fake, seeded_ids):
    rid = seeded_ids["recording_id"]
    fake.canned["submit_recording_research"] = OFFICIAL
    run = lomax.run_now("recording", "recording", subject_id=rid, api_key="k")
    props = {p["field"]: p for p in lomax.get_run(run.id)["result"]["proposals"]}
    lomax.decide(props["title"]["id"], "accepted", User.query.first().id)
    rec = db.session.get(Recording, rid)
    assert rec.kind == "studio" and rec.title.startswith("An Evening with")


def test_album_run_with_no_title_gets_tracks_and_identify_instruction(app, fake, tmp_path):
    cur = {"artist": "Herbie Hancock", "title": "", "year": "",
           "date": "1978-02-12",
           "tracks": [{"number": 1, "title": "Maiden Voyage", "duration": 612, "songwriter": ""},
                      {"number": 2, "title": "Oh Mommy", "duration": 301, "songwriter": ""}],
           "info_file_content": "", "notes": ""}
    lomax.run_now("album", "folder", subject_key=str(tmp_path / "hh"), current=cur, api_key="k")
    text = user_text(fake.calls[-1])
    assert "Identify the release first" in text
    assert "Maiden Voyage [10:12]" in text and "Oh Mommy" in text
    assert "title: (none filed)" in text and "year: 1978" in text
    assert "Identify the release" not in user_text(fake.calls[-1]).replace("Identify the release first", "")
    assert "identify the release first" in SKILLS["album"].instructions.lower()
