"""
tests/test_lomax_fixes.py -- the 2026-10-05 Lomax fixes: state abbreviations, track notes, lineage
as the recording chain only, orphaned "no question" sentences, genre (Lomax and the MusicBrainz
suggestion), and the stored decision being the truth after a reload. No network.
"""
import pytest

import app.lomax as lomax
from app.extensions import db
from app.lomax import filters
from app.lomax.skills import SKILLS
from app.lomax.skills.base import Subject
from app.models.artist import Artist
from app.models.genre import Genre
from app.models.lomax import LomaxProposal, LomaxRun
from app.models.recording import Recording
from app.models.track import Track

from tests.test_lomax_api import api  # noqa: F401
from tests.test_lomax_core import fake, run_skill  # noqa: F401

pytestmark = pytest.mark.usefixtures("fake")

REC = SKILLS["recording"]


def _norm(raw, current=None, **scratch):
    subj = Subject("folder", key="/x", current=current or {})
    subj.scratch.update(scratch)
    return REC.normalize(dict({"thinking": "", "proposals": [], "tracks": []}, **raw), subj)


# ── 2. state and province ────────────────────────────────────────────────────

@pytest.mark.parametrize("given,want", [
    ("Tennessee", "TN"), ("tn", "TN"), ("Tenn.", "TN"), ("N.Y.", "NY"), ("District of Columbia", "DC"),
    ("Ontario", "ON"), ("Quebec", "QC"), ("Québec", "QC"), ("Newfoundland and Labrador", "NL"),
    ("New South Wales", "NSW"), ("Western Australia", "WA"), ("Australian Capital Territory", "ACT"),
    ("Northern Territory", "NT"), ("Bavaria", "Bavaria"), ("", "")])
def test_state_code(given, want):
    assert filters.state_code(given) == want


def test_a_state_proposal_is_normalised_to_the_abbreviation():
    _, props = _norm({"proposals": [{"field": "state", "proposed": "Tennessee", "confidence": "high", "source": "web"}]},
                     current={"state": ""})
    assert props[0]["proposed"] == "TN" and props[0]["agrees"] is False


@pytest.mark.parametrize("filed", ["TN", "Tennessee", "tn"])
def test_a_state_equal_after_normalising_is_an_agreement(filed):
    _, props = _norm({"proposals": [{"field": "state", "proposed": "Tennessee", "confidence": "high", "source": "web"}]},
                     current={"state": filed})
    assert props[0]["agrees"] is True and props[0]["proposed"] == "TN"


def test_the_prompt_states_the_abbreviation_rule():
    flat = __import__("app.lomax.prompts", fromlist=["BASE"]).BASE.replace("\\\n", " ").replace("\n", " ")
    assert "State is always the 2-letter abbreviation" in flat


# ── 3. track notes ───────────────────────────────────────────────────────────

INFO = "Go Kurosawa\n01. Opening Jam\n02. Tuning\n03. Nardis *\n04. Band intro\n\n* with Jerry Garcia on pedal steel\n"
CUR = {"info_file_content": INFO, "tracks": [{"number": n, "title": t, "songwriter": "", "notes": ""}
                                              for n, t in ((1, "Opening Jam"), (2, "Tuning"), (3, "Nardis"), (4, "Band intro"))]}


def _notes(**notes):
    result, props = _norm({"tracks": [{"number": int(k[1:]), "title": CUR["tracks"][int(k[1:]) - 1]["title"], "note": v}
                                      for k, v in notes.items()]}, current=CUR)
    return {p["field"]: p["proposed"] for p in props if p["field"].endswith(".note")}, result


def test_a_note_the_info_file_footnote_supports_is_kept():
    got, result = _notes(n3="with Jerry Garcia on pedal steel")
    assert got == {"track.3.note": "with Jerry Garcia on pedal steel"}
    assert result["tracks"][0]["note"] == "with Jerry Garcia on pedal steel"


@pytest.mark.parametrize("n,note", [
    (2, "Tuning"), (4, "Band intro"), (1, "Opening instrumental improvisation"), (4, "Crowd banter and applause"),
    (3, "Extended jam with a false start and a long tempo change"),     # nothing in the info file supports it
    (3, "x" * 141)])
def test_self_evident_unsupported_and_long_notes_are_dropped(n, note):
    got, result = _notes(**{"n%d" % n: note})
    assert got == {} and result["tracks"][0]["note"] == ""


def test_a_note_is_dropped_when_there_is_no_info_file_text():
    result, props = _norm({"tracks": [{"number": 3, "title": "Nardis", "note": "with Jerry Garcia on pedal steel"}]},
                          current={"tracks": CUR["tracks"], "info_file_content": ""})
    assert not [p for p in props if p["field"].endswith(".note")]


def test_a_filed_note_is_not_a_reason_to_keep_a_note():
    cur = {"info_file_content": "", "tracks": [{"number": 3, "title": "Nardis", "notes": "Live debut"}]}
    result, props = _norm({"tracks": [{"number": 3, "title": "Nardis", "note": "Live debut"}]}, current=cur)
    assert not [p for p in props if p["field"] == "track.3.note"] and result["tracks"][0]["note"] == ""


def test_titles_and_songwriters_are_not_filtered():
    _, props = _norm({"tracks": [{"number": 1, "title": "Opening Jam", "songwriter": "Go Kurosawa"}]}, current=CUR)
    assert {p["field"] for p in props} == {"track.1.title", "track.1.songwriter"}


def test_the_prompt_limits_notes():
    ins = REC.instructions.replace("\\\n", " ")
    assert "exists ONLY to carry what the info file says about that track" in ins
    for forbidden in ("describes what the track is", "tuning, intro, band intro, banter, applause, crowd, announcer",
                      "album, release, year", "anything not written next to that track or in its footnote",
                      "No exceptions for helpful context"):
        assert forbidden in ins


# ── 6. lineage ───────────────────────────────────────────────────────────────

# The chain, then the performance notes the model copied along with it.
GO_MIXED = ("Schoeps CMC6 > Sound Devices MixPre-6 > Audacity (normalized, split) > FLAC 16/44.1. "
            "Caveat: Go said this was their first performance of the program, so expect some rough edges.")
GO_CHAIN = "Schoeps CMC6 > Sound Devices MixPre-6 > Audacity (normalized, split) > FLAC 16/44.1."


def _lineage(filed, proposed, flag=False):
    _, props = _norm({"proposals": [{"field": "lineage", "proposed": proposed, "confidence": "medium",
                                     "source": "info_file", "agrees": flag}]}, current={"lineage": filed})
    return props[0] if props else None


def test_a_lineage_proposal_that_copies_performance_notes_is_cut_to_the_chain():
    p = _lineage(GO_MIXED, GO_MIXED, flag=True)          # even flagged as an agreement
    assert p["proposed"] == GO_CHAIN.rstrip(".") and p["agrees"] is False
    assert "Caveat" not in p["proposed"] and "performance" not in p["proposed"]


def test_a_lineage_that_is_only_the_chain_is_an_agreement():
    p = _lineage(GO_CHAIN.rstrip("."), GO_CHAIN)
    assert p["agrees"] is True


def test_a_lineage_with_no_chain_in_it_is_dropped():
    assert _lineage("", "Caveat: Go said this was their first performance.") is None


def test_the_prompt_says_lineage_is_only_the_chain():
    flat = __import__("app.lomax.prompts", fromlist=["BASE"]).BASE.replace("\\\n", " ").replace("\n", " ")
    assert "technical path the recording took" in flat and "and nothing else" in flat
    assert "Discrepancies and explanations about lineage go in verify_items or thinking" in flat


# ── 7. orphaned "no question" ────────────────────────────────────────────────

ORPHAN = "No question was asked; this run focuses on the recording's details and track listing."


def test_a_sentence_about_the_absence_of_a_question_is_removed():
    assert filters.drop_absent_question_sentences(ORPHAN) == ""
    assert filters.drop_absent_question_sentences("The date is 1980. " + ORPHAN + " The venue matches.") == \
        "The date is 1980. The venue matches."
    assert filters.drop_absent_question_sentences("Since you didn't ask anything specific, here is a summary.") == ""
    assert filters.drop_absent_question_sentences("There was no specific question, so I checked the details.") == ""
    assert filters.drop_absent_question_sentences("The Sprague show is the right one.") == \
        "The Sprague show is the right one."


def test_normalize_leaves_no_trace_of_a_question_that_was_not_asked():
    result, _ = _norm({"answer": ORPHAN, "thinking": ORPHAN + " Date and venue agree."})
    assert result["answer"] == "" and result["thinking"] == "Date and venue agree."


def test_an_answer_survives_when_a_question_was_asked():
    result, _ = _norm({"answer": "It is the early show."}, question="Which show is this?")
    assert result["answer"] == "It is the early show."


def test_a_run_without_a_question_stores_no_answer(fake, seeded_ids):
    fake.canned["submit_recording_research"] = {"thinking": ORPHAN, "answer": ORPHAN, "proposals": []}
    run = run_skill("recording", "recording", seeded_ids["recording_id"])
    r = lomax.get_run(run.id)["result"]
    assert r["answer"] == "" and "question" not in r["thinking"].lower()


def test_the_other_skills_drop_it_too(fake, seeded_ids):
    fake.canned["submit_artist_history"] = {"thinking": ORPHAN, "answer": ORPHAN, "biography": "A bio."}
    run = run_skill("artist", "artist", seeded_ids["artist_id"])
    r = lomax.get_run(run.id)["result"]
    assert r["answer"] == "" and r["thinking"] == ""


def test_the_prompt_ties_the_answer_to_a_question():
    flat = __import__("app.lomax.prompts", fromlist=["BASE"]).BASE.replace("\\\n", " ").replace("\n", " ")
    assert "'answer' exists only when the archivist attached a question" in flat


# ── 8. genre: Lomax ──────────────────────────────────────────────────────────

def _genre_run(fake, rid, proposed):
    fake.canned["submit_recording_research"] = {"thinking": "t", "proposals": [
        {"field": "genre", "proposed": proposed, "confidence": "medium", "source": "web"}]}
    return run_skill("recording", "recording", rid)


def test_lomax_proposes_a_genre_for_an_act_with_none_choosing_an_existing_one(fake, seeded_ids):
    db.session.add(Genre(name="Jazz"))
    db.session.commit()
    run = _genre_run(fake, seeded_ids["recording_id"], "jazz, bebop")
    prompt = fake.calls[-1]["messages"][0]["content"]
    assert "Genre is open" in prompt and "Trellis genres" in prompt and "Jazz" in prompt
    p = db.session.query(LomaxProposal).filter_by(run_id=run.id, field="genre").one()
    assert p.proposed == "Jazz" and p.agrees is False


def test_no_genre_is_proposed_for_an_act_that_has_one(fake, seeded_ids):
    g = Genre(name="Jazz")
    db.session.add(g)
    db.session.flush()
    db.session.get(Artist, seeded_ids["artist_id"]).genre_id = g.id
    db.session.commit()
    run = _genre_run(fake, seeded_ids["recording_id"], "Bluegrass")
    assert "Genre is open" not in fake.calls[-1]["messages"][0]["content"]
    assert db.session.query(LomaxProposal).filter_by(run_id=run.id, field="genre").count() == 0


def test_accepting_a_genre_fills_the_acts_genre_and_never_overwrites(api, fake, seeded_ids):
    db.session.add(Genre(name="Jazz"))
    db.session.commit()
    run = _genre_run(fake, seeded_ids["recording_id"], "Jazz")
    p = db.session.query(LomaxProposal).filter_by(run_id=run.id, field="genre").one()
    r = api.post("/api/lomax/proposals/%d" % p.id, json={"decision": "accepted"})
    assert r.status_code == 200, r.get_json()
    artist = db.session.get(Artist, seeded_ids["artist_id"])
    assert artist.genre.name == "Jazz"
    # A person set another genre meanwhile: a second proposal cannot replace it.
    other = Genre(name="Bluegrass")
    db.session.add(other)
    db.session.flush()
    artist.genre_id = other.id
    p2 = LomaxProposal(run_id=run.id, field="genre", proposed="Jazz", confidence="low")
    db.session.add(p2)
    db.session.commit()
    r = api.post("/api/lomax/proposals/%d" % p2.id, json={"decision": "accepted"})
    assert r.status_code >= 400 and db.session.get(Artist, seeded_ids["artist_id"]).genre.name == "Bluegrass"
    assert db.session.get(LomaxProposal, p2.id).decision is None


def test_a_genre_for_a_folder_run_is_judged_against_the_page_and_the_library(fake, app):
    from app.lomax import evidence as ev
    assert ev.act_genre_name({"artist": "Nobody Known", "genre": ""}) == ""
    assert ev.act_genre_name({"artist": "Nobody Known", "genre": "Folk"}) == "Folk"


# ── 8. genre: the MusicBrainz suggestion for the attended flows ──────────────

@pytest.fixture
def mb(monkeypatch):
    from app.utils import musicbrainz as m
    m._GENRE_CACHE.clear()
    monkeypatch.setattr(m, "enabled", lambda: True)
    calls = {"search": 0, "genres": 0}

    def search(name, limit=6):
        calls["search"] += 1
        return [{"mbid": "mb1", "score": 100, "name": name}, {"mbid": "mb2", "score": 40, "name": "x"}]

    def genres(rg, mbid=None):
        calls["genres"] += 1
        return ["hard bop", "jazz"]
    monkeypatch.setattr(m, "search_artist", search)
    monkeypatch.setattr(m, "release_genres", genres)
    yield calls
    m._GENRE_CACHE.clear()


def test_the_suggestion_matches_an_existing_genre_and_is_cached(api, mb):
    db.session.add(Genre(name="Jazz"))
    db.session.commit()
    r = api.get("/api/ingest/genre-suggestion?artist_name=Brand New Act").get_json()
    assert r["genre"]["name"] == "Jazz" and r["genre"]["genre_id"]
    api.get("/api/ingest/genre-suggestion?artist_name=brand new act")
    assert mb["search"] == 1 and mb["genres"] == 1                # the second ask came from the cache
    assert db.session.query(Genre).count() == 1                   # reading creates nothing


def test_the_suggestion_names_a_new_genre_without_creating_it(api, mb):
    r = api.get("/api/ingest/genre-suggestion?artist_name=Brand New Act").get_json()
    assert r["genre"] == {"genre_id": None, "name": "Hard Bop"}
    assert db.session.query(Genre).count() == 0


def test_an_act_that_already_has_a_genre_gets_no_suggestion(api, mb, seeded_ids):
    g = Genre(name="Jazz")
    db.session.add(g)
    db.session.flush()
    db.session.get(Artist, seeded_ids["artist_id"]).genre_id = g.id
    db.session.commit()
    r = api.get("/api/ingest/genre-suggestion?artist_name=Bill Evans").get_json()
    assert r["genre"] is None and mb["search"] == 0


def test_the_suggestion_is_quietly_empty_offline_or_disabled(api, monkeypatch):
    from app.utils import musicbrainz as m
    m._GENRE_CACHE.clear()
    assert api.get("/api/ingest/genre-suggestion?artist_name=Anybody").get_json() == {"genre": None}   # TESTING: off
    monkeypatch.setattr(m, "enabled", lambda: True)
    monkeypatch.setattr(m, "tripped", lambda: True)
    assert api.get("/api/ingest/genre-suggestion?artist_name=Anybody").get_json() == {"genre": None}


def test_a_failed_lookup_is_not_cached_as_a_real_none(api, monkeypatch):
    from app.utils import musicbrainz as m
    m._GENRE_CACHE.clear()
    monkeypatch.setattr(m, "enabled", lambda: True)

    def failing(name, limit=6):
        m._failures[0] += 1
        return []
    monkeypatch.setattr(m, "search_artist", failing)
    try:
        assert m.suggest_genre("Somebody") is None
        assert m._GENRE_CACHE == {}
    finally:
        m._failures[0] = 0


def test_filling_through_apply_genre_still_means_one_genre_fill_if_empty(app, seeded_ids):
    from app.utils import musicbrainz as m
    artist = db.session.get(Artist, seeded_ids["artist_id"])
    g = m.apply_genre_to_artist(artist, ["hard bop"])
    assert g.name == "Hard Bop" and artist.genre_id == g.id
    assert m.apply_genre_to_artist(artist, ["jazz"]) is None and artist.genre_id == g.id


# ── 9a. similar acts ─────────────────────────────────────────────────────────

def test_similar_acts_finds_an_ampersand_variant(api):
    db.session.add(Artist(name="Bela Fleck & Edgar Meyer"))
    db.session.commit()
    names = [a["name"] for a in api.get("/api/ingest/similar-acts?artist_name=Bela Fleck and Edgar Meyer").get_json()["acts"]]
    assert "Bela Fleck & Edgar Meyer" in names
    assert api.get("/api/ingest/similar-acts?artist_name=").get_json() == {"acts": []}


# ── 5. the stored decision is the truth ──────────────────────────────────────

def test_an_accepted_folder_proposal_is_still_accepted_after_a_reload(api, fake):
    fake.canned["submit_recording_research"] = {"thinking": "t", "proposals": [
        {"field": "venue", "proposed": "Barley's Tap Room", "confidence": "high", "source": "web"}],
        "tracks": [{"number": 1, "title": "Nardis", "songwriter": "Miles Davis"}]}
    run = lomax.run_now("recording", "folder", subject_key="/imports/go", api_key="k", level="study",
                        current={"venue": "Barley", "tracks": [{"number": 1, "title": "Track 1"}]})
    props = {p.field: p for p in db.session.get(LomaxRun, run.id).proposals}
    for f in ("venue", "track.1.title"):
        assert api.post("/api/lomax/proposals/%d" % props[f].id, json={"decision": "accepted"}).status_code == 200
    # "Reload the payload": what the Resolver loads on return carries the decision AND the value,
    # which is all the page needs to put the value back into the form.
    reloaded = api.get("/api/lomax/runs?skill=recording&subject_type=folder&subject_key=/imports/go").get_json()["runs"]
    got = {p["field"]: p for p in reloaded[-1]["result"]["proposals"]}
    assert got["venue"]["decision"] == "accepted" and got["venue"]["proposed"] == "Barley's Tap Room"
    assert got["track.1.title"]["decision"] == "accepted" and got["track.1.title"]["proposed"] == "Nardis"
    assert got["track.1.songwriter"]["decision"] is None


# ── review round: genres, show identity, anchored notes, no-question false positives, states, lineage ──

@pytest.mark.parametrize("given,want", [
    ("R&B", "R&B"), ("Rhythm and Blues", "Rhythm and Blues"), ("Rock & Roll", "Rock & Roll"),
    ("hard bop / bebop", "hard bop"), ("Jazz, Fusion", "Jazz"), ("jazz; funk", "jazz")])
def test_one_genre_keeps_whole_names_and_splits_only_lists(app, given, want):
    from app.lomax.skills.recording import _one_genre
    assert _one_genre(given) == want


def test_one_genre_prefers_an_existing_genres_own_spelling(app):
    from app.lomax.skills.recording import _one_genre
    db.session.add_all([Genre(name="R&B"), Genre(name="Rhythm and Blues"), Genre(name="Jazz")])
    db.session.commit()
    assert _one_genre("r&b") == "R&B"
    assert _one_genre("rhythm and blues") == "Rhythm and Blues"
    assert _one_genre("jazz / bebop") == "Jazz"


@pytest.mark.parametrize("bad", ["A very long genre name with many words", "Funk 2000", "Great jazz. Really!", "What: jazz"])
def test_a_new_genre_name_must_be_short_and_plain(app, bad):
    from app.lomax.skills.recording import _one_genre
    assert _one_genre(bad) == ""


def test_musicbrainz_spelling_is_kept_not_title_cased_away(app):
    from app.utils import musicbrainz as m
    assert m.pick_genre(["r&b"]) == (None, "R&B")
    assert m.pick_genre(["drum and bass"]) == (None, "Drum and Bass")
    assert m.pick_genre(["hard bop"]) == (None, "Hard Bop")
    assert m.pick_genre(["2-step garage"]) == (None, None)        # a digit: not a new genre


def _act_with_genre():
    from app.models import Genre as _G
    a, b = Genre(name="Bluegrass"), Genre(name="Jazz")
    act = Artist(name="Hot Rize")
    db.session.add_all([a, b, act])
    db.session.commit()
    act.genre_id = a.id
    return act, a, b


def test_a_hand_pick_replaces_the_acts_genre(app):
    from app.api.ingest import _apply_artist_genre
    act, a, b = _act_with_genre()
    _apply_artist_genre(act, {"genre_id": b.id, "genre_source": "hand"})
    assert act.genre_id == b.id
    _apply_artist_genre(act, {"genre_name": "Brand New", "genre_source": "hand"})
    assert act.genre.name == "Brand New"


def test_an_accepted_suggestion_never_replaces_the_acts_genre(app):
    from app.api.ingest import _apply_artist_genre
    act, a, b = _act_with_genre()
    _apply_artist_genre(act, {"genre_id": b.id, "genre_source": "suggestion"})
    _apply_artist_genre(act, {"genre_name": "Brand New", "genre_source": "suggestion"})
    assert act.genre_id == a.id and db.session.query(Genre).filter_by(name="Brand New").count() == 0


def test_an_accepted_suggestion_fills_an_empty_genre(app):
    from app.api.ingest import _apply_artist_genre
    b = Genre(name="Jazz")
    empty = Artist(name="No Genre Yet")
    db.session.add_all([b, empty])
    db.session.commit()
    _apply_artist_genre(empty, {"genre_id": b.id, "genre_source": "suggestion"})
    assert empty.genre_id == b.id


def test_a_missing_genre_source_fills_only_an_empty_genre(app):
    from app.api.ingest import _apply_artist_genre
    act, a, b = _act_with_genre()
    _apply_artist_genre(act, {"genre_id": b.id})
    assert act.genre_id == a.id
    empty = Artist(name="No Genre Yet")
    db.session.add(empty)
    db.session.commit()
    _apply_artist_genre(empty, {"genre_id": b.id})
    assert empty.genre_id == b.id


def test_a_folder_run_carries_the_shows_fingerprint(api, fake):
    run = lomax.run_now("recording", "folder", subject_key="/imports/go", api_key="k", level="study",
                        current={"fingerprint": "2.abc.def", "tracks": []})
    assert lomax.get_run(run.id)["fingerprint"] == "2.abc.def"


# anchored notes
ANCH = ("Go Kurosawa\n01. Opening Jam\n02. Nardis *\n03. Wolverine Blues **\n04. Tuning\n\n"
        "* JG on pedal steel\n** Bob Weir on guitar\n")
ANCH_CUR = {"info_file_content": ANCH, "tracks": [{"number": n, "title": t, "songwriter": "", "notes": ""}
                                                   for n, t in ((1, "Opening Jam"), (2, "Nardis"), (3, "Wolverine Blues"), (4, "Tuning"))]}


def _anch(number, note):
    title = ANCH_CUR["tracks"][number - 1]["title"]
    _, props = _norm({"tracks": [{"number": number, "title": title, "note": note}]}, current=ANCH_CUR)
    return [p["proposed"] for p in props if p["field"].endswith(".note")]


def test_a_note_is_matched_against_the_footnote_its_own_mark_points_to():
    assert _anch(2, "with Jerry Garcia on pedal steel") == ["with Jerry Garcia on pedal steel"]   # JG fits Jerry Garcia
    assert _anch(3, "Bob Weir on guitar") == ["Bob Weir on guitar"]


def test_words_from_a_different_footnote_do_not_support_a_note():
    assert _anch(3, "Bob Weir on pedal steel") == []          # Bob Weir is footnote **, pedal steel is footnote *
    assert _anch(2, "Bob Weir on guitar") == []               # track 2 points at footnote *


def test_a_track_line_with_no_mark_and_no_annotation_supports_no_note():
    assert _anch(1, "with Jerry Garcia on pedal steel") == []
    assert _anch(4, "Bob Weir on guitar") == []


def test_an_inline_annotation_on_the_track_line_supports_a_note():
    cur = {"info_file_content": "01. Nardis (with Miles Davis on trumpet)\n", "tracks": [{"number": 1, "title": "Nardis"}]}
    _, props = _norm({"tracks": [{"number": 1, "title": "Nardis", "note": "with Miles Davis on trumpet"}]}, current=cur)
    assert any(p["field"] == "track.1.note" for p in props)


# the no-question filter keeps ordinary sentences
@pytest.mark.parametrize("keep", [
    "There is no question that this was Garcia's finest night.",
    "Jerry did not ask for a drumstick.",
    "The unprompted announcement drew a laugh.",
    "Nobody asked, but the encore was long."])
def test_ordinary_sentences_survive_the_no_question_filter(keep):
    assert filters.drop_absent_question_sentences(keep) == keep


@pytest.mark.parametrize("gone", [
    "No question was asked.", "No question was provided, so here is a summary.", "You didn't ask anything.",
    "You did not ask a question.", "Without a specific question, I checked the details.",
    "Since no question was given, this is a summary."])
def test_sentences_about_the_missing_question_go(gone):
    assert filters.drop_absent_question_sentences(gone) == ""


# states and the country
def test_the_country_decides_which_state_list_applies():
    assert filters.state_code("Georgia", "Georgia") == "Georgia"          # the country, not the state
    assert filters.state_code("Georgia", "US") == "GA"
    assert filters.state_code("Georgia", "United States") == "GA"
    assert filters.state_code("Victoria", "Canada") == "Victoria"         # not a Canadian province name
    assert filters.state_code("Victoria", "Australia") == "VIC"
    assert filters.state_code("Ontario", "Germany") == "Ontario"
    assert filters.state_code("Tennessee") == "TN"                        # unknown country, unambiguous name


def test_shared_codes_stay_as_given_until_a_country_decides():
    assert filters.state_code("wa") == "wa" and filters.state_code("NT") == "NT"
    assert filters.state_code("wa", "AU") == "WA" and filters.state_code("wa", "US") == "WA"
    assert filters.state_code("tn") == "TN"


def test_ordinary_words_are_not_state_aliases():
    assert filters.state_code("Man") == "Man" and filters.state_code("Mont") == "Mont"


def test_the_country_reaches_the_state_proposal():
    _, props = _norm({"proposals": [{"field": "state", "proposed": "Georgia", "confidence": "high", "source": "web"}]},
                     current={"state": "", "country": "Georgia"})
    assert props[0]["proposed"] == "Georgia"


# lineage
def test_one_arrow_does_not_bypass_the_commentary_check():
    assert filters.clean_lineage("Guest Branford Marsalis sat in on Saturday > Sunday") == ""
    assert filters.clean_lineage("AUD > FLAC. Guest Branford Marsalis sat in on Saturday > Sunday") == "AUD > FLAC"
    assert filters.clean_lineage("Schoeps > MixPre > Audacity (the show opener) > FLAC") != ""


# ── junk notes: Ryan's own examples, at normalize and at read time ──────────

RYAN_INFO = ("Go Kurosawa\n01. Tuning:\n02. Blues on the Corner\n03. Announcer\n04. Applause\n"
             "05. Nardis *\n\n* with Jerry Garcia on pedal steel\n")
RYAN_TRACKS = [(1, "Tuning:"), (2, "Blues on the Corner"), (3, "Announcer"), (4, "Applause"), (5, "Nardis")]
RYAN_JUNK = {1: "Brief tuning segment, not a performed piece", 2: "From The Real McCoy (1967)",
             3: "Announcer introduces the band and thanks the crowd", 4: "Extended applause from the audience"}
RYAN_CUR = {"info_file_content": RYAN_INFO,
            "tracks": [{"number": n, "title": t, "songwriter": "", "notes": ""} for n, t in RYAN_TRACKS]}


@pytest.mark.parametrize("number", sorted(RYAN_JUNK))
def test_ryans_junk_notes_are_dropped_at_normalize(number):
    title = dict(RYAN_TRACKS)[number]
    result, props = _norm({"tracks": [{"number": number, "title": title, "note": RYAN_JUNK[number]}]}, current=RYAN_CUR)
    assert not [p for p in props if p["field"].endswith(".note")] and result["tracks"][0]["note"] == ""


def test_the_footnote_note_still_passes_next_to_the_junk():
    result, props = _norm({"tracks": [{"number": n, "title": t, "note": RYAN_JUNK.get(n, "with Jerry Garcia on pedal steel")}
                                      for n, t in RYAN_TRACKS]}, current=RYAN_CUR)
    assert [p["proposed"] for p in props if p["field"].endswith(".note")] == ["with Jerry Garcia on pedal steel"]


@pytest.mark.parametrize("line,note", [
    ("02. Blues on the Corner 4:59", "Brief tuning segment"),                                  # a duration is no annotation
    ("02. Blues on the Corner (4:59)", "Brief tuning segment"),
    ("02. Blues on the Corner - from the Real McCoy", "From the Real McCoy"),                 # plain text, not parenthetical
])
def test_only_a_parenthetical_that_is_not_a_duration_counts(line, note):
    got = filters.filter_track_note(note, "Blues on the Corner", line + "\n", None, 2)
    assert got == ""


def test_an_album_written_beside_the_track_in_the_info_file_is_the_info_files_own_text():
    info = "02. Blues on the Corner (From The Real McCoy 1967)\n"
    assert filters.filter_track_note("From The Real McCoy 1967", "Blues on the Corner", info, None, 2)
    assert filters.filter_track_note("From The Real McCoy (1967)", "Blues on the Corner", "02. Blues on the Corner\n", None, 2) == ""


def test_a_parenthetical_note_is_kept_word_for_word_and_nothing_more():
    info = "02. Blues on the Corner (with Jerry Garcia on pedal steel)\n"
    assert filters.filter_track_note("with Jerry Garcia on pedal steel", "Blues on the Corner", info, None, 2)
    assert filters.filter_track_note("with Jerry Garcia on pedal steel, from 1967", "Blues on the Corner", info, None, 2) == ""


def _stored_run(app):
    import json
    notes = {n: RYAN_JUNK[n] for n in RYAN_JUNK}
    notes[5] = "with Jerry Garcia on pedal steel"
    result = {"thinking": "", "answer": "", "tracks": [{"number": n, "title": t, "songwriter": "", "note": notes.get(n, "")}
                                                         for n, t in RYAN_TRACKS],
              "proposals": []}
    run = LomaxRun(skill="recording", subject_type="folder", subject_key="/imports/go", level="study", status="done",
                   result_json=json.dumps(result), input_json=json.dumps(RYAN_CUR))
    db.session.add(run)
    db.session.flush()
    for n, note in notes.items():
        db.session.add(LomaxProposal(run_id=run.id, field="track.%d.note" % n, proposed=note, current=""))
    db.session.add(LomaxProposal(run_id=run.id, field="track.2.title", proposed="Blues on the Corner", current=""))
    db.session.commit()
    return run


def _only_the_footnote_note(d):
    fields = [(p["field"], p["proposed"]) for p in d["result"]["proposals"]]
    assert ("track.5.note", "with Jerry Garcia on pedal steel") in fields
    assert [f for f, _ in fields if f.endswith(".note")] == ["track.5.note"]
    assert ("track.2.title", "Blues on the Corner") in fields          # titles are untouched
    assert [t["note"] for t in d["result"]["tracks"]] == ["", "", "", "", "with Jerry Garcia on pedal steel"]


def test_old_stored_runs_are_cleaned_when_served_and_the_row_is_not_changed(app):
    run = _stored_run(app)
    stored_before = run.result_json
    _only_the_footnote_note(lomax.get_run(run.id))
    _only_the_footnote_note(lomax.list_runs("recording", "folder", subject_key="/imports/go")[-1])
    assert db.session.get(LomaxRun, run.id).result_json == stored_before
    assert db.session.query(LomaxProposal).filter_by(run_id=run.id).count() == 6      # nothing deleted


def test_a_saved_recordings_stored_run_is_cleaned_with_its_own_info_file(app, seeded_ids):
    import json
    rec = db.session.get(Recording, seeded_ids["recording_id"])
    rec.info_file_content = RYAN_INFO
    result = {"tracks": [{"number": 1, "title": "Tuning:", "note": RYAN_JUNK[1]},
                         {"number": 5, "title": "Nardis", "note": "with Jerry Garcia on pedal steel"}], "proposals": []}
    run = LomaxRun(skill="recording", subject_type="recording", subject_id=rec.id, level="study", status="done",
                   result_json=json.dumps(result))
    db.session.add(run)
    db.session.commit()
    for d in (lomax.latest_run("recording", "recording", rec.id), lomax.latest_result("recording", rec.id, ("recording",))):
        got = d["result"] if "result" in d else d
        assert [t["note"] for t in got["tracks"]] == ["", "with Jerry Garcia on pedal steel"]


def test_the_notes_filter_runs_on_the_endpoints_too(api, app):
    run = _stored_run(app)
    body = api.get("/api/lomax/runs/%d" % run.id).get_json()
    assert [t["note"] for t in body["result"]["tracks"]][:4] == ["", "", "", ""]
    listed = api.get("/api/lomax/runs?skill=recording&subject_type=folder&subject_key=/imports/go").get_json()["runs"]
    assert not [p for p in listed[-1]["result"]["proposals"] if p["field"].endswith(".note") and p["field"] != "track.5.note"]


# ── lineage as chain steps ───────────────────────────────────────────────────

JARRETT = ("Audience recording (Mono, Sound A-) per uploader's notes; the Dutch-radio/Michiel de Ruyter broadcast "
           "note in the same info file actually refers to a separate, unconfirmed May 10 1976 Utrecht/Tivoli Hall "
           "show, not this Milwaukee recording")


def test_ryans_lineage_paragraph_does_not_survive_as_a_sentence():
    assert filters.clean_lineage(JARRETT) in ("", "AUD")
    assert filters.clean_lineage([JARRETT]) in ("", "AUD")


@pytest.mark.parametrize("chain", [
    "Schoeps MK4 > Sonosax SX-M2 > Sony D-8 (DAT) > Tascam DA-30 > CDR > EAC > FLAC", "AUD > DAT",
    "Sony ECM-909 > Sony TC-D5M > DAT > CDR > EAC > FLAC 16/44.1"])
def test_a_real_chain_is_kept(chain):
    assert filters.clean_lineage(chain) == chain
    assert filters.clean_lineage(chain.split(" > ")) == chain


def test_steps_that_are_sentences_or_reports_or_dates_or_places_are_dropped():
    steps = ["AUD", "per uploader's notes", "Recorded at Tivoli Hall", "May 10 1976", "Dutch radio broadcast refers to another show",
             "DAT", "A very long step that goes on and on well past the sixty character limit DAT", "Taper: Bob Smith", "FLAC 16/44.1"]
    assert filters.clean_lineage(steps) == "AUD > DAT > Taper: Bob Smith > FLAC 16/44.1"


def test_the_model_submits_lineage_as_a_list_and_the_app_joins_it():
    _, props = _norm({"lineage": ["AUD", "DAT", "Sony ECM-909 > Sony TC-D5M", "refers to a separate show"]},
                     current={"lineage": ""})
    lin = [p for p in props if p["field"] == "lineage"]
    assert len(lin) == 1 and lin[0]["proposed"] == "AUD > DAT > Sony ECM-909 > Sony TC-D5M" and lin[0]["challenge"] is False


def test_a_list_wins_over_a_lineage_proposal_in_the_old_string_form():
    _, props = _norm({"lineage": ["AUD", "DAT"], "proposals": [
        {"field": "lineage", "proposed": JARRETT, "confidence": "medium", "source": "info_file"}]}, current={"lineage": ""})
    assert [p["proposed"] for p in props if p["field"] == "lineage"] == ["AUD > DAT"]


def test_lineage_that_is_all_explanation_is_dropped_whole():
    _, props = _norm({"lineage": [JARRETT]}, current={"lineage": ""})
    assert not [p for p in props if p["field"] == "lineage"]


def test_the_schema_takes_lineage_as_a_list_and_not_as_a_proposal_field():
    props = REC.submit_tool["input_schema"]["properties"]
    assert props["lineage"]["type"] == "array" and props["lineage"]["items"] == {"type": "string"}
    assert "lineage" not in props["proposals"]["items"]["properties"]["field"]["enum"]
    ins = REC.instructions.replace("\\\n", " ")
    assert "Lineage is the technical path the recording took" in ins and "never in lineage" in ins


def test_old_stored_lineage_paragraphs_are_cleaned_when_served_and_the_row_is_not_changed(app):
    import json
    result = {"proposals": [], "tracks": []}
    run = LomaxRun(skill="recording", subject_type="folder", subject_key="/imports/kj", level="study", status="done",
                   result_json=json.dumps(result), input_json=json.dumps({"tracks": []}))
    db.session.add(run)
    db.session.flush()
    db.session.add(LomaxProposal(run_id=run.id, field="lineage", proposed=JARRETT, current=""))
    db.session.add(LomaxProposal(run_id=run.id, field="lineage", proposed=GO_MIXED, current=""))
    db.session.commit()
    got = [p["proposed"] for p in lomax.get_run(run.id)["result"]["proposals"] if p["field"] == "lineage"]
    assert got == [GO_CHAIN.rstrip(".")]
    assert db.session.query(LomaxProposal).filter_by(run_id=run.id).count() == 2
    assert db.session.query(LomaxProposal).filter_by(run_id=run.id, field="lineage").first().proposed == JARRETT


# ── genre for the act the recording will be filed under ──────────────────────

def _jarrett_run(fake, rid, artist_filed, **extra):
    fake.canned["submit_recording_research"] = {"thinking": "t", "proposals": [
        {"field": "artist", "proposed": "Keith Jarrett", "current": artist_filed, "confidence": "high", "source": "web"},
        {"field": "genre", "proposed": "Jazz", "confidence": "high", "source": "web"}], **extra}
    return run_skill("recording", "folder", None, subject_key="/imports/kj", level="study",
                     current={"artist": artist_filed, "tracks": []}) if rid is None else None


def test_a_genre_is_proposed_for_the_artist_lomax_proposes_when_the_filed_one_is_a_misread(app, fake):
    run = _jarrett_run(fake, None, "SURVIVORS SUITE Concerts")
    assert fake.calls[-1].get("tools") is not None
    assert not [t for t in fake.calls[-1]["tools"] if t.get("name") == "web_search"]      # Study: no search needed
    got = [p for p in lomax.get_run(run.id)["result"]["proposals"] if p["field"] == "genre"]
    assert len(got) == 1 and got[0]["proposed"] == "Jazz"


def test_a_genre_is_judged_for_the_proposed_artist_when_the_filed_act_has_one(app, fake):
    g = Genre(name="Rock")
    act = Artist(name="Some Rock Band")
    db.session.add_all([g, act])
    db.session.flush()
    act.genre_id = g.id
    db.session.commit()
    run = _jarrett_run(fake, None, "Some Rock Band")
    assert [p["proposed"] for p in lomax.get_run(run.id)["result"]["proposals"] if p["field"] == "genre"] == ["Jazz"]


def test_no_genre_when_the_proposed_artist_already_has_one(app, fake):
    g = Genre(name="Jazz")
    kj = Artist(name="Keith Jarrett")
    db.session.add_all([g, kj])
    db.session.flush()
    kj.genre_id = g.id
    db.session.commit()
    run = _jarrett_run(fake, None, "SURVIVORS SUITE Concerts")
    assert not [p for p in lomax.get_run(run.id)["result"]["proposals"] if p["field"] == "genre"]


def test_the_genre_prompt_says_general_knowledge_and_primary_genre():
    ins = REC.instructions.replace("\\\n", " ")
    assert "general knowledge and needs no search" in ins and "propose its primary genre" in ins
    assert "judge the genre for that artist" in ins


# ── track table styling ──────────────────────────────────────────────────────

def _css():
    import os
    with open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "css", "main.css"), encoding="utf-8") as f:
        return f.read()


def test_the_track_table_is_one_step_smaller_with_hairlines_and_a_smaller_radius():
    import re
    css = _css()
    rule = lambda sel: re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css).group(1)      # noqa: E731
    table = rule(".lx-tracks")
    assert "font-size: 11.5px" in table and "border-radius: var(--r-sm)" in table       # was 12px and 6px
    assert "border: 1px solid var(--bd-0)" in rule(".lx-tracks th, .lx-tracks td")
    sizes = set(re.findall(r"font-size: (\d+(?:\.\d+)?)px", css))
    for used in re.findall(r"font-size: (\d+(?:\.\d+)?)px", "".join(rule(s) for s in (".lx-tracks", ".lx-tracks .lx-pk", ".lx-tracks thead th"))):
        assert used in sizes and used in ("10", "10.5", "11.5")                          # only sizes the scale already has
