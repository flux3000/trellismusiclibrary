"""
tests/test_lomax_api.py -- /api/lomax, accepting and rejecting proposals, a folder run re-pointing
to its recording on save, and the old AI Assist and Dossier routes working through their
wrappers. No network (the fake client from test_lomax_core; runs finish inline).
"""
import json

import pytest

import app.lomax as lomax
from app.extensions import db
from app.models.artist import Artist, ArtistResource
from app.models.lomax import LomaxProposal, LomaxRun
from app.models.musician import Membership
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.recording_event import RecordingEvent
from app.models.user import User
from app.models.venue import Venue

from tests.test_lomax_core import fake  # noqa: F401

pytestmark = pytest.mark.usefixtures("fake")


@pytest.fixture
def api(app, monkeypatch):
    app.config["LOMAX_INLINE"] = True
    monkeypatch.setattr("app.api.lomax.get_api_key", lambda uid: "k")
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(User.query.first().id)
        s["_fresh"] = True
    return client


def _proposals(run_id):
    return {p.field: p for p in db.session.get(LomaxRun, run_id).proposals}


def _make_run(fake, skill, st, sid, canned, **kw):
    name = {"recording": "submit_recording_research",
            "artist": "submit_artist_history", "venue": "submit_venue_history"}[skill]
    fake.canned[name] = canned
    return lomax.run_now(skill, st, subject_id=sid, api_key="k", **kw)


# ── the endpoints ────────────────────────────────────────────────────────────

def test_post_runs_without_a_key_asks_for_one(app, monkeypatch, seeded_ids):
    monkeypatch.setattr("app.api.lomax.get_api_key", lambda uid: None)
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(User.query.first().id)
        s["_fresh"] = True
    r = client.post("/api/lomax/runs", json={"skill": "artist", "subject_type": "artist",
                                             "subject_id": seeded_ids["artist_id"]})
    assert r.status_code == 428 and r.get_json()["error"] == "no_api_key"


def test_a_run_is_created_polled_and_listed_as_latest(api, fake, seeded_ids):
    fake.canned["submit_recording_research"] = {"thinking": "t", "proposals": [
        {"field": "lineage", "proposed": "DAT > FLAC", "confidence": "low", "source": "info_file"}]}
    r = api.post("/api/lomax/runs", json={"skill": "recording", "subject_type": "recording",
                                          "subject_id": seeded_ids["recording_id"], "level": "study",
                                          "question": "Which DAT?"})
    assert r.status_code == 202
    run_id = r.get_json()["id"]
    got = api.get("/api/lomax/runs/%d" % run_id).get_json()
    assert got["status"] == "done" and got["level"] == "study" and got["question"] == "Which DAT?"
    assert got["result"]["proposals"][0]["id"] and got["result"]["proposals"][0]["decision"] is None
    latest = api.get("/api/lomax/latest?skill=recording&subject_type=recording&subject_id=%d"
                     % seeded_ids["recording_id"]).get_json()
    assert latest["id"] == run_id
    assert api.get("/api/lomax/latest?skill=recording&subject_type=recording&subject_id=999").get_json() is None
    assert api.get("/api/lomax/runs/9999").status_code == 404


def test_bad_run_requests_are_400_or_404(api, seeded_ids):
    assert api.post("/api/lomax/runs", json={"skill": "nope", "subject_type": "recording",
                                             "subject_id": 1}).status_code == 400
    assert api.post("/api/lomax/runs", json={"skill": "venue", "subject_type": "venue",
                                             "subject_id": 999}).status_code == 404
    assert api.post("/api/lomax/runs", json={"skill": "recording", "subject_type": "folder"}).status_code == 400


def test_estimate_endpoint(api):
    got = api.get("/api/lomax/estimate?skill=artist&level=read").get_json()
    assert got["max_searches"] == 0 and got["low_tokens"] < got["high_tokens"]
    assert api.get("/api/lomax/estimate?skill=nope").status_code == 400


# ── accepting and rejecting ──────────────────────────────────────────────────

def test_accepting_applies_the_value_and_records_the_decision(api, fake, seeded_ids):
    rid = seeded_ids["recording_id"]
    db.session.get(Recording, rid).resolver_json = json.dumps({f: {"value": "x", "confidence": "tentative"}
                                                               for f in ("date", "state", "stage")})
    db.session.commit()
    run = _make_run(fake, "recording", "recording", rid, {"thinking": "t", "proposals": [
        {"field": "date", "proposed": "1980-03-01", "confidence": "high", "source": "web"},
        {"field": "state", "proposed": "MA", "confidence": "low", "source": "web"},
        {"field": "stage", "proposed": "Main", "confidence": "low", "source": "web"}]})
    props = _proposals(run.id)
    r = api.post("/api/lomax/proposals/%d" % props["date"].id, json={"decision": "accepted"})
    assert r.status_code == 200 and r.get_json()["decision"] == "accepted" and r.get_json()["decided_at"]
    perf = db.session.get(Performance, seeded_ids["performance_id"])
    assert (perf.start_year, perf.start_month, perf.start_day) == (1980, 3, 1)
    # Location lands on the real linked venue, as the pages do.
    api.post("/api/lomax/proposals/%d" % props["state"].id, json={"decision": "accepted"})
    assert db.session.get(Venue, perf.venue_id).state == "MA"
    api.post("/api/lomax/proposals/%d" % props["stage"].id, json={"decision": "accepted"})
    assert db.session.get(Performance, perf.id).stage == "Main"
    # Counts as human-set: logged on the recording.
    notes = [e.note for e in db.session.query(RecordingEvent).filter_by(recording_id=rid)]
    assert any("Lomax proposal accepted: date" in (n or "") for n in notes)


def test_rejecting_records_the_decision_and_changes_nothing(api, fake, seeded_ids):
    run = _make_run(fake, "recording", "recording", seeded_ids["recording_id"], {
        "thinking": "t", "proposals": [
            {"field": "source", "proposed": "SBD", "confidence": "low", "source": "web"}]})
    p = _proposals(run.id)["source"]
    r = api.post("/api/lomax/proposals/%d" % p.id, json={"decision": "rejected"})
    assert r.get_json()["decision"] == "rejected"
    assert db.session.get(Recording, seeded_ids["recording_id"]).source == "AUD"


def test_accepting_a_source_and_a_venue_and_an_artist(api, fake, seeded_ids):
    rid = seeded_ids["recording_id"]
    run = _make_run(fake, "recording", "recording", rid, {"thinking": "t", "proposals": [
        {"field": "source", "proposed": "SBD", "confidence": "medium", "source": "web"}]})
    api.post("/api/lomax/proposals/%d" % _proposals(run.id)["source"].id, json={"decision": "accepted"})
    assert db.session.get(Recording, rid).source == "SBD"

    run = _make_run(fake, "recording", "recording", rid, {"thinking": "t", "proposals": [
        {"field": "venue", "proposed": "Yale Hall", "confidence": "medium", "source": "web"},
        {"field": "event", "proposed": "Winter Series", "confidence": "medium", "source": "web"}]})
    # the seeded recording has no resolver reading, so event and stage are empty and in scope; venue is not
    props = _proposals(run.id)
    assert props["venue"].challenge is True      # the venue was filed, so this challenges it
    api.post("/api/lomax/proposals/%d" % props["event"].id, json={"decision": "accepted"})
    perf = db.session.get(Performance, seeded_ids["performance_id"])
    assert perf.event.name == "Winter Series"


def test_accepting_an_invalid_value_is_a_400_and_leaves_it_undecided(api, fake, seeded_ids):
    run = _make_run(fake, "recording", "recording", seeded_ids["recording_id"], {"thinking": "t", "proposals": [
        {"field": "stage", "proposed": "x", "confidence": "low", "source": "web"}]})
    p = _proposals(run.id)["stage"]
    p.field, p.proposed = "date", "last Tuesday"
    db.session.commit()
    r = api.post("/api/lomax/proposals/%d" % p.id, json={"decision": "accepted"})
    assert r.status_code == 400 and "ISO" in r.get_json()["error"]
    assert db.session.get(LomaxProposal, p.id).decision is None


def test_a_bad_decision_word_is_400(api, fake, seeded_ids):
    run = _make_run(fake, "venue", "venue", db.session.get(Performance, seeded_ids["performance_id"]).venue_id, {
        "thinking": "t", "history": "h", "proposals": [
            {"field": "city", "proposed": "Hamden", "confidence": "low", "source": "web"}]})
    pid = _proposals(run.id)["city"].id
    assert api.post("/api/lomax/proposals/%d" % pid, json={"decision": "maybe"}).status_code == 400
    assert api.post("/api/lomax/proposals/99999", json={"decision": "accepted"}).status_code == 404
    assert api.post("/api/lomax/proposals/%d" % pid, json={"decision": "accepted"}).status_code == 200
    assert db.session.get(Venue, db.session.get(Performance, seeded_ids["performance_id"]).venue_id).city == "Hamden"


def test_accepting_twice_applies_once(api, fake, seeded_ids):
    run = _make_run(fake, "artist", "artist", seeded_ids["artist_id"], {
        "thinking": "t", "biography": "b", "members": [], "resources": [
            {"label": "Fan archive", "url": "https://fans.example/be"}]})
    pid = _proposals(run.id)["resource"].id
    api.post("/api/lomax/proposals/%d" % pid, json={"decision": "accepted"})
    api.post("/api/lomax/proposals/%d" % pid, json={"decision": "accepted"})
    assert db.session.query(ArtistResource).filter_by(artist_id=seeded_ids["artist_id"]).count() == 1


def test_accepting_member_proposals_fills_a_blank_stint_or_adds_a_person(api, fake, seeded_ids):
    aid = seeded_ids["artist_id"]
    run = _make_run(fake, "artist", "artist", aid, {"thinking": "t", "biography": "b", "resources": [],
        "members": [{"name": "bill evans", "start": "1950", "end": "1980-09", "instrument": "piano",
                     "confidence": "high", "url": "https://x.example/1"},
                    {"name": "Scott LaFaro", "start": "1959", "end": "1961", "instrument": "bass",
                     "confidence": "medium", "url": "https://x.example/2"}]})
    for p in db.session.get(LomaxRun, run.id).proposals:
        assert api.post("/api/lomax/proposals/%d" % p.id, json={"decision": "accepted"}).status_code == 200
    rows = {m.musician.name: m for m in db.session.query(Membership).filter_by(artist_id=aid)}
    assert len(rows) == 2
    be = rows["Bill Evans"]                      # the unbounded stint was filled in, not duplicated
    assert (be.start_year, be.end_year, be.end_month, be.instrument) == (1950, 1980, 9, "piano")
    sl = rows["Scott LaFaro"]
    assert (sl.start_year, sl.end_year, sl.instrument) == (1959, 1961, "bass")
    assert db.session.get(Artist, aid).confirmed_at is not None


def test_a_proposal_on_a_folder_run_is_recorded_only(api, fake, tmp_path):
    fake.canned["submit_recording_research"] = {"thinking": "t", "proposals": [
        {"field": "venue", "proposed": "Somewhere", "confidence": "low", "source": "web"}]}
    run = lomax.run_now("recording", "folder", subject_key=str(tmp_path), current={"artist": "X"}, api_key="k")
    p = run.proposals[0]
    assert api.post("/api/lomax/proposals/%d" % p.id, json={"decision": "accepted"}).status_code == 200
    assert db.session.query(Venue).filter_by(name="Somewhere").count() == 0


# ── a folder run follows its folder to the recording ─────────────────────────

def test_a_folder_run_is_repointed_when_the_import_saves_the_recording(app, tmp_path, monkeypatch, fake):
    import numpy as np
    import soundfile as sf
    from mutagen.flac import FLAC
    from app.api import ingest

    show = tmp_path / "import" / "Pat Metheny Group - 1979-06-14 - Stars (SBD)"
    show.mkdir(parents=True)
    for i in (1, 2):
        sf.write(str(show / ("%02d.flac" % i)), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
        a = FLAC(str(show / ("%02d.flac" % i)))
        a["ARTIST"], a["DATE"] = "Pat Metheny Group", "1979-06-14"
        a.save()
    library = tmp_path / "library"
    library.mkdir()
    app.config["LIBRARY_ROOT"] = str(library)
    monkeypatch.setattr(ingest, "verdict", lambda r: ("ingested", []))

    run = lomax.run_now("recording", "folder", subject_key=str(show), current={"artist": "PMG"}, api_key="k")
    assert (run.subject_type, run.subject_key) == ("folder", str(show))
    out = ingest.auto_confirm(str(show), User.query.first().id)
    rec_id = out["result"]["recording_id"]

    run = db.session.get(LomaxRun, run.id)
    assert (run.subject_type, run.subject_id, run.subject_key, run.input_json) == ("recording", rec_id, None, None)
    # and the recording's page now shows it, in the shape it always read
    assert lomax.latest_result("recording", rec_id, ("recording",))["thinking"] == "t"


def test_a_result_the_page_holds_is_filed_as_a_done_run_when_no_run_exists(app, seeded_ids):
    result = {"thinking": "held", "proposals": [{"field": "source", "proposed": "SBD", "confidence": "low"}],
              "model": "m", "usage": {"total_tokens": 5}}
    lomax.adopt_result(seeded_ids["recording_id"], result, None)
    db.session.commit()
    got = lomax.latest_run("recording", "recording", seeded_ids["recording_id"])
    assert got["status"] == "done" and got["result"]["thinking"] == "held"
    assert got["result"]["proposals"][0]["field"] == "source"


# ── the pages' read side (the old AI Assist and Dossier routes are gone) ─────

def test_the_legacy_wrapper_routes_are_gone(app):
    rules = [r.rule for r in app.url_map.iter_rules()]
    assert not [r for r in rules if "ai-assist" in r or "/dossier" in r], rules


def test_the_run_list_is_oldest_first_and_includes_a_run_still_working(api, fake, seeded_ids):
    rid = seeded_ids["recording_id"]
    fake.canned["submit_recording_research"] = {"thinking": "t", "answer": "A1", "proposals": []}
    first = lomax.run_now("recording", "recording", subject_id=rid, api_key="k", question="One?")
    queued = lomax.create_run("recording", "recording", subject_id=rid, question="Two?")
    got = api.get("/api/lomax/runs?skill=recording&subject_type=recording&subject_id=%d" % rid).get_json()["runs"]
    assert [r["id"] for r in got] == [first.id, queued.id]
    assert got[0]["result"]["answer"] == "A1" and got[1]["status"] == "queued"
    assert api.get("/api/lomax/runs?skill=recording&subject_type=recording").status_code == 400
    assert api.get("/api/lomax/runs?skill=astrology&subject_type=recording&subject_id=1").status_code == 400


def test_a_folder_runs_are_listed_by_key(api, fake, tmp_path):
    r = api.post("/api/lomax/runs", json={"skill": "recording", "subject_type": "folder",
                                          "subject_key": str(tmp_path), "current": {"artist": "A"}})
    assert r.status_code == 202
    got = api.get("/api/lomax/runs?skill=recording&subject_type=folder&subject_key=%s" % tmp_path).get_json()["runs"]
    assert [x["id"] for x in got] == [r.get_json()["id"]]


def test_the_page_reads_a_recordings_and_an_artists_runs_back(api, fake, seeded_ids):
    aid = seeded_ids["artist_id"]
    _make_run(fake, "artist", "artist", aid, {
        "thinking": "t", "biography": "A pianist.",
        "members": [{"name": "Scott LaFaro", "confidence": "high", "url": "https://x.example"}],
        "resources": [{"label": "L", "url": "https://x.example/l"}]})
    page = api.get("/api/artists/%d" % aid).get_json()
    assert page["bio"] == "A pianist."                      # the biography auto-applies
    est = api.get("/api/artists/ai-estimate").get_json()
    assert {"low_tokens", "high_tokens", "max_searches"} <= set(est)


def test_the_peer_catalog_still_carries_the_dossier(app, fake, seeded_ids):
    from app.api.share import lomax as share_lomax
    assert share_lomax is lomax
    run = _make_run(fake, "artist", "artist", seeded_ids["artist_id"],
                    {"thinking": "t", "biography": "Shared bio.", "members": []})
    assert lomax.latest_result("artist", seeded_ids["artist_id"], ("artist",), not_mode="lineup")["biography"] == "Shared bio."


def test_accepting_a_former_name_adds_a_venue_alias_once(api, fake, seeded_ids):
    from app.models.alias import VenueAlias
    vid = db.session.get(Performance, seeded_ids["performance_id"]).venue_id
    run = _make_run(fake, "venue", "venue", vid, {"thinking": "t", "history": "h", "proposals": [],
                    "former_names": [{"name": "Old Music Hall", "from": "1921", "to": "1950"}]})
    pid = _proposals(run.id)["former_name"].id
    assert api.post("/api/lomax/proposals/%d" % pid, json={"decision": "accepted"}).status_code == 200
    assert api.post("/api/lomax/proposals/%d" % pid, json={"decision": "accepted"}).status_code == 200
    rows = db.session.query(VenueAlias).filter_by(venue_id=vid).all()
    assert [r.alias for r in rows] == ["Old Music Hall"]


def test_a_venues_history_can_be_edited_by_hand(api, seeded_ids):
    """Lomax writes History; the page lets the archivist edit it like Notes (and Restore previous
    only offers itself while the text is still what the run wrote)."""
    vid = db.session.get(Performance, seeded_ids["performance_id"]).venue_id
    assert api.put("/api/venues/%d" % vid, json={"history": "Opened 1921."}).status_code == 200
    assert api.get("/api/venues/%d" % vid).get_json()["history"] == "Opened 1921."
