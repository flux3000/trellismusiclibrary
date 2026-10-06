"""
tests/test_lomax_skills.py -- what each skill sends, in what order, and what it keeps.
Internal evidence first (Known already, Reference matches, Read from the files), then trusted
sources, then the open web; Read sends no web tool; a rejected proposal comes back to the next
run labelled rejected. No network (the fake client from test_lomax_core).
"""
import json
import types

import pytest

import app.lomax as lomax
from app.extensions import db
from app.lomax import evidence
from app.models.artist import Artist, ArtistResource
from app.models.lomax import LomaxProposal
from app.models.recording import Recording

from tests.test_lomax_core import fake, run_skill, user_text, web_tool  # noqa: F401

pytestmark = pytest.mark.usefixtures("fake")


class FakeAtlas:
    def artist(self, text, **kw):
        return [types.SimpleNamespace(name="Bill Evans", matched="Bill Evans", score=1.0)]

    def venue(self, text, **kw):
        return [types.SimpleNamespace(id=7, name="Sprague Hall", matched="Sprague", score=0.9)]

    def event(self, text, **kw):
        return []

    def event_place(self, artist, date):
        return [{"date": "1980-02-22", "act": "Bill Evans", "place": "Sprague Hall", "city": "New Haven",
                 "exact": True}]

    def place_info(self, pid):
        return {"city": "New Haven", "region": "CT", "country": "US"}

    def place_names(self, pid):
        return [("Sprague Memorial Hall", "official", None, None), ("Old Music Hall", "former", 1900, 1950)]


@pytest.fixture
def atlas(monkeypatch):
    monkeypatch.setattr(evidence, "_atlas", lambda: FakeAtlas())


def _order(text, *heads):
    idx = [text.index(h) for h in heads]
    assert idx == sorted(idx), dict(zip(heads, idx))


def _resolver(rec_id, **overrides):
    f = lambda v, c="confident", **kw: dict(value=v, confidence=c, **kw)   # noqa: E731
    data = {"artist": f("Bill Evans"), "date": f({"year": 1980, "month": 2, "day": 22}),
            "venue": f("Sprague", "tentative", source="info",
                       evidence=[{"source": "info", "text": "Sprague hall", "line": 2}],
                       runner_up={"value": "Sprague Memorial Hall", "source": "folder"}),
            "city": f(None, "empty"), "state": f("CT"), "country": f("US"),
            "event": f("Winter Series"), "stage": f("Main"), "status": "review",
            "reasons": ["tentative:venue"]}
    data.update(overrides)
    rec = db.session.get(Recording, rec_id)
    rec.resolver_json = json.dumps(data)
    db.session.commit()


# ── Resolution Enhancement ───────────────────────────────────────────────────

def test_resolution_prompt_carries_the_internal_evidence_in_order(fake, atlas, seeded_ids):
    _resolver(seeded_ids["recording_id"])
    run_skill("recording", "recording", seeded_ids["recording_id"])
    text = user_text(fake.calls[-1])
    _order(text, "Fields in scope", "Known already", "Reference matches", "Read from the files",
           "Trusted sources")
    assert "Fields in scope (tentative or empty): venue, city" in text
    assert "venue: Sprague [tentative; from info] IN SCOPE" in text
    assert "runner-up: Sprague Memorial Hall (from folder)" in text
    assert "Resolver verdict: review (tentative:venue)" in text
    assert "Old Music Hall 1900-1950" in text                      # Atlas former names, with years
    assert "Atlas show on 1980-02-22" in text
    assert "Venue in the library: Sprague Memorial Hall (New Haven, CT, US)" in text
    assert "setlist" not in text.split("Trusted sources")[0].lower()


def test_resolution_read_level_sends_no_web_tool(fake, atlas, seeded_ids):
    _resolver(seeded_ids["recording_id"])
    run_skill("recording", "recording", seeded_ids["recording_id"], level="study")
    assert web_tool(fake.calls[-1]) is None


def test_resolution_keeps_confident_field_proposals_flagged_as_challenges(fake, seeded_ids):
    _resolver(seeded_ids["recording_id"])
    fake.canned["submit_recording_research"] = {"thinking": "t", "proposals": [
        {"field": "date", "proposed": "1980-03-01", "confidence": "medium", "source": "web"},
        {"field": "venue", "proposed": "Sprague Memorial Hall", "confidence": "medium", "source": "atlas"},
        {"field": "lineage", "proposed": "DAT > FLAC", "confidence": "low", "source": "web"}]}
    run = run_skill("recording", "recording", seeded_ids["recording_id"])
    got = {p["field"]: p for p in lomax.get_run(run.id)["result"]["proposals"]}
    assert set(got) == {"date", "venue", "lineage"}
    assert got["lineage"]["challenge"] is False          # lineage is not a resolver field
    assert got["date"]["challenge"] is True              # the resolver was confident about the date
    assert got["venue"]["challenge"] is False            # tentative, so in scope
    assert db.session.query(LomaxProposal).filter_by(field="date").one().challenge is True


def test_resolution_with_nothing_tentative_still_builds_a_prompt(fake, seeded_ids):
    all_confident = {k: {"value": "x", "confidence": "confident"}
                     for k in ("artist", "date", "venue", "city", "state", "country", "event", "stage")}
    rec = db.session.get(Recording, seeded_ids["recording_id"])
    rec.resolver_json = json.dumps(all_confident)
    db.session.commit()
    run = run_skill("recording", "recording", seeded_ids["recording_id"])
    assert run.status == "done" and len(fake.calls) == 1
    assert "The resolver settled every field. Look only for known corrections" in user_text(fake.calls[-1])


def test_resolution_folder_subject_uses_what_the_page_sent(fake, app, atlas, tmp_path):
    current = {"artist": "Bill Evans", "date": "1980-02-22", "venue": "", "resolved": {
        "venue": {"value": None, "confidence": "empty"}}}
    run = lomax.run_now("recording", "folder", subject_key=str(tmp_path), current=current, api_key="k")
    assert run.status == "done"
    text = user_text(fake.calls[-1])
    assert "venue" in text.split("Known already")[0]
    assert "Other recording of this artist on this date: #1" in text      # the library, not the folder


# ── Recording Research ───────────────────────────────────────────────────────

def test_recording_prompt_has_known_fields_tracks_info_then_trusted(fake, app, seeded_ids):
    rec = db.session.get(Recording, seeded_ids["recording_id"])
    rec.info_file_content = "Set I\nMy Foolish Heart\n"
    db.session.commit()
    run_skill("recording", "recording", seeded_ids["recording_id"])
    text = user_text(fake.calls[-1])
    _order(text, "Known already", "Tracks on disk (2)", "Info file contents", "Trusted sources")
    assert "1. My Foolish Heart [5:00]" in text                    # durations
    assert "venue: Sprague Memorial Hall" in text and "source: AUD" in text


def test_recording_keeps_proposals_for_all_ten_fields_and_drops_unknown_ones(fake, seeded_ids):
    fake.canned["submit_recording_research"] = {"thinking": "t", "proposals": [
        {"field": "lineage", "proposed": "DAT > FLAC", "confidence": "low", "source": "info_file"},
        {"field": "date", "proposed": "1981-01-01", "confidence": "high", "source": "web"},
        {"field": "shnid", "proposed": "123", "confidence": "high", "source": "web"}]}
    run = run_skill("recording", "recording", seeded_ids["recording_id"])
    assert sorted(p["field"] for p in lomax.get_run(run.id)["result"]["proposals"]) == ["date", "lineage"]


def test_recording_instructions_keep_the_setlist_parsing_rules():
    from app.lomax.skills import SKILLS
    ins = SKILLS["recording"].instructions
    for rule in ("Segue notation", "Footnote markers", "Section labels", "only a time value".replace("only a", "is only a"),
                 "Named improvisation"):
        assert rule in ins, rule


# ── the recap of earlier runs ────────────────────────────────────────────────

def test_a_rejected_proposal_is_listed_as_rejected_in_the_next_run(fake, seeded_ids):
    rid = seeded_ids["recording_id"]
    _resolver(rid)
    fake.canned["submit_recording_research"] = {"thinking": "t", "proposals": [
        {"field": "venue", "proposed": "Wrong Hall", "confidence": "low", "source": "web"},
        {"field": "city", "proposed": "Hartford", "confidence": "medium", "source": "web"}]}
    first = run_skill("recording", "recording", rid)
    props = {p.field: p for p in first.proposals}
    lomax.decide(props["venue"].id, "rejected")
    lomax.decide(props["city"].id, "rejected")
    assert "PREVIOUS RUN" not in user_text(fake.calls[-1])

    fake.canned["submit_recording_research"] = {"thinking": "t", "proposals": [
        {"field": "stage", "proposed": "Hall", "confidence": "low", "source": "web"}]}
    run_skill("recording", "recording", rid)
    second = user_text(fake.calls[-1])
    assert "venue -> Wrong Hall (low, REJECTED by the archivist)" in second
    assert "corroborates nothing" in second                        # the recap is not a second source

    run_skill("recording", "recording", rid)
    third = user_text(fake.calls[-1])
    # The latest run proposed only stage; the earlier rejections still come back.
    assert "venue -> Wrong Hall (REJECTED by the archivist, earlier run)" in third
    assert "city -> Hartford (REJECTED by the archivist, earlier run)" in third


def test_a_prior_run_with_no_proposals_adds_no_recap(fake, seeded_ids):
    rid = seeded_ids["recording_id"]
    run_skill("recording", "recording", rid)
    run_skill("recording", "recording", rid)
    assert "PREVIOUS RUN" not in user_text(fake.calls[-1])


def test_a_migrated_legacy_run_feeds_the_recap(fake, seeded_ids):
    from app.models.lomax import LomaxRun
    rid = seeded_ids["recording_id"]
    db.session.add(LomaxRun(skill="recording", subject_type="recording", subject_id=rid, status="done",
                            result_json=json.dumps({"proposals": [
                                {"field": "date", "proposed": "1978-12-31", "confidence": "high"}],
                                "verify_items": ["Track 4 title uncertain"]}), migrated_from="ai_research_json"))
    db.session.commit()
    run_skill("recording", "recording", rid)
    text = user_text(fake.calls[-1])
    assert "date -> 1978-12-31 (high)" in text and "unresolved: Track 4 title uncertain" in text


# ── Artist History ───────────────────────────────────────────────────────────

def test_artist_prompt_sends_facts_roster_links_and_the_draft_in_order(fake, app, seeded_ids):
    a = db.session.get(Artist, seeded_ids["artist_id"])
    a.mb_type, a.mb_area, a.mb_begin = "Person", "Plainfield", "1929"
    a.bio = "An old draft."
    a.mb_links_json = json.dumps({"setlist.fm": "https://www.setlist.fm/x", "Official site": "https://be.example",
                                  "Streaming": "https://stream.example/be", "Buy": "https://buy.example/be"})
    db.session.add(ArtistResource(artist_id=a.id, label="Fan archive", url="https://fans.example/be"))
    db.session.commit()
    run_skill("artist", "artist", a.id)
    text = user_text(fake.calls[-1])
    _order(text, "Known already", "Trusted sources", "Existing biography draft")
    for needle in ("Origin: Plainfield", "Active from: 1929", "Bill Evans", "Recordings in the library: 1",
                   "https://fans.example/be", "https://www.setlist.fm/x", "https://be.example",
                   "etreedb.org", "archive.org/details/etree", "bluegrassarchive.com", "gdarchive.net",
                   "jerrybase.com", "phish.net"):
        assert needle in text, needle
    assert "stream.example" not in text and "buy.example" not in text      # Streaming and Buy are skipped


def test_artist_history_is_one_pass_and_its_biography_auto_applies(fake, seeded_ids):
    fake.canned["submit_artist_history"] = {
        "thinking": "t", "biography": "He played piano.",
        "members": [{"name": "Scott LaFaro", "start": "1959", "end": "1961", "confidence": "high",
                     "url": "https://example.org/r"}],
        "resources": [{"label": "Fan archive", "url": "https://fans.example/be"}]}
    run = run_skill("artist", "artist", seeded_ids["artist_id"])
    assert len(fake.calls) == 1
    assert db.session.get(Artist, seeded_ids["artist_id"]).bio == "He played piano."
    got = lomax.get_run(run.id)["result"]
    assert got["applied"] == {"biography": True}
    assert sorted(p["field"] for p in got["proposals"]) == ["member", "resource"]


def test_only_artist_and_venue_history_auto_apply(fake, seeded_ids):
    from app.lomax.skills import SKILLS
    assert [k for k, s in SKILLS.items() if s.apply_auto] == ["artist", "venue"]
    before = db.session.get(Recording, seeded_ids["recording_id"]).source
    run_skill("recording", "recording", seeded_ids["recording_id"])
    assert db.session.get(Recording, seeded_ids["recording_id"]).source == before


def test_artist_read_level_sends_no_web_tool(fake, seeded_ids):
    run_skill("artist", "artist", seeded_ids["artist_id"], level="study")
    assert web_tool(fake.calls[-1]) is None


def test_an_empty_biography_does_not_wipe_the_description(fake, seeded_ids):
    a = db.session.get(Artist, seeded_ids["artist_id"])
    a.bio = "Keep me."
    db.session.commit()
    fake.canned["submit_artist_history"] = {"thinking": "t", "biography": "", "members": []}
    run_skill("artist", "artist", a.id)
    assert db.session.get(Artist, a.id).bio == "Keep me."


# ── Venue History ────────────────────────────────────────────────────────────

def test_venue_prompt_has_library_then_atlas_names_then_trusted(fake, atlas, seeded_ids):
    from app.models.performance import Performance
    vid = db.session.get(Performance, seeded_ids["performance_id"]).venue_id
    run_skill("venue", "venue", vid)
    text = user_text(fake.calls[-1])
    _order(text, "Known already", "Reference matches", "Trusted sources")
    assert "Recordings in the library at this venue: 1, first 1980-02-22, last 1980-02-22" in text
    assert "Acts recorded there: Bill Evans" in text
    assert "Old Music Hall 1900-1950" in text


def test_venue_run_keeps_history_former_names_and_location_proposals(fake, seeded_ids):
    from app.models.performance import Performance
    vid = db.session.get(Performance, seeded_ids["performance_id"]).venue_id
    fake.canned["submit_venue_history"] = {
        "thinking": "t", "history": "Opened 1921.", "former_names": [{"name": "Old Hall", "from": "1921"}],
        "proposals": [{"field": "city", "proposed": "Hamden", "confidence": "low", "source": "web"},
                      {"field": "venue", "proposed": "x", "confidence": "low", "source": "web"}]}
    run = run_skill("venue", "venue", vid)
    got = lomax.get_run(run.id)["result"]
    assert got["history"] == "Opened 1921." and got["former_names"][0]["name"] == "Old Hall"
    assert [p["field"] for p in got["proposals"]] == ["city", "former_name"]


# ── bad input ────────────────────────────────────────────────────────────────

def test_a_skill_refuses_a_subject_type_it_does_not_work_on(seeded_ids):
    with pytest.raises(lomax.LomaxError):
        lomax.create_run("artist", "recording", subject_id=seeded_ids["recording_id"])
    with pytest.raises(lomax.LomaxError):
        lomax.create_run("astrology", "recording", subject_id=1)
    with pytest.raises(lomax.LomaxNotFound):
        lomax.create_run("venue", "venue", subject_id=9999)
    with pytest.raises(lomax.LomaxError):
        lomax.create_run("recording", "recording", subject_id=seeded_ids["recording_id"], level="deep")
