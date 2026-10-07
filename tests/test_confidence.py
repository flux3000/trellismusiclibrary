"""
tests/test_confidence.py -- Resolver v2 chunk 6: calibrated confidence, the verdict's
tentative reasons, and resolver_json carrying evidence.

No audio, no database, no network: scans are hand-built dicts, the fitting runs on
synthetic samples.
"""
import json
import math

import pytest

from app.utils.reader import calibrate as cal
from app.utils.reader import confidence as C
from app.utils.resolve import Field, Resolved, resolve, verdict


# ── fitting ──────────────────────────────────────────────────────────────────

def _samples(n=400, seed_mod=7):
    """Synthetic: a higher logit is right more often; two populations; groups of 5."""
    out = []
    for i in range(n):
        logit = (i * 37 % 200) / 10.0                       # 0.0 .. 19.9
        wrong = (i % 50 == 0) if logit > 12 else (i % 3 == 0)
        for pop in ("none", "loo"):
            out.append({"item": str(i), "group": f"a{i % seed_mod}", "pop": pop, "logit": logit,
                        "margin": logit * 0.9, "has_value": i % 11 != 0, "wrong": wrong})
    return out


def test_folds_are_deterministic_and_never_split_a_group():
    groups = [f"a{i % 9}" for i in range(100)]
    one, two = cal.assign_folds(groups, 5), cal.assign_folds(list(reversed(groups)), 5)
    assert one == two
    assert set(one) == set(groups)
    assert set(one.values()) <= set(range(5))


def test_fit_is_deterministic_given_fixed_inputs():
    s = _samples()
    assert cal.fit_model(s) == cal.fit_model(list(s))
    folds = cal.assign_folds([x["group"] for x in s], 5)
    assert cal.cross_validate(s, folds) == cal.cross_validate(s, folds)


def test_bins_are_monotone_in_the_logit():
    bins = cal.make_bins(_samples())
    ps = [b["p"] for b in bins]
    assert ps == sorted(ps)
    assert bins[0]["lo"] is None and bins[-1]["hi"] is None


def test_fit_keeps_confident_wrong_under_the_training_target_in_every_population():
    s = _samples(800)
    model = cal.fit_model(s, train_target=0.01)
    for pop, r in cal.evaluate(model, s).items():
        assert r["rate"] <= 0.01, pop
    assert model["tau"] < 1.01 and any(r["confident"] for r in cal.evaluate(model, s).values())


def test_a_field_with_too_little_data_is_never_confident_from_what_the_corpus_holds():
    small = [x for x in _samples(60)]
    model = cal.fit_model(small)
    assert model["min_logit"] > max(x["logit"] for x in small)
    assert all(r["confident"] == 0 for r in cal.evaluate(model, small).values())
    assert cal.fit_model([])["min_logit"] == cal.NEVER


# ── applying thresholds ──────────────────────────────────────────────────────

_MODEL = {"bins": [{"lo": None, "hi": 4, "n": 10, "right": 5, "p": 0.5},
                   {"lo": 4, "hi": None, "n": 10, "right": 10, "p": 0.99}], "tau": 0.9, "m": 2.0}


def test_threshold_application():
    assert C.classify(_MODEL, 6.0, 3.0) == (0.99, "confident")
    assert C.classify(_MODEL, 6.0, 1.9)[1] == "tentative"      # margin short
    assert C.classify(_MODEL, 3.9, 9.0)[1] == "tentative"      # probability short
    floor = dict(_MODEL, min_logit=8.0)
    assert C.classify(floor, 7.9, 9.0)[1] == "tentative"
    assert C.classify(floor, 8.0, 9.0)[1] == "confident"


def test_none_candidate_competes_and_can_win():
    cal_doc = {"fields": {"artist": _MODEL}}
    a = C.assess("artist", {"x": -1.0}, "x", cal_doc)
    assert a.confidence == "empty" and a.none_wins
    a = C.assess("artist", {"x": 8.0, "y": 7.5}, "x", cal_doc)
    assert a.confidence == "tentative" and a.runner_key == "y" and a.margin == pytest.approx(0.5)
    a = C.assess("artist", {"x": 8.0, "y": 1.0}, "x", cal_doc)
    assert a.confidence == "confident" and a.p == 0.99


def test_logit_is_score_against_every_rival_and_none():
    logit, margin, runner = C.raw_scores({"x": 5.0, "y": 3.0}, "x", "artist")
    assert margin == pytest.approx(2.0) and runner == "y"
    assert logit == pytest.approx(5.0 - math.log(math.exp(3.0) + math.exp(C.NONE_SCORE["artist"])))


# ── the shipped calibration file ─────────────────────────────────────────────

def test_calibration_file_matches_the_evidence_tables_and_names_its_corpora():
    doc = C.load_calibration(force=True)
    assert doc["none_scores"] == C.NONE_SCORE, "evidence tables changed: refit with --calibrate --write-calibration"
    assert doc["tables"] == {"date_weight": C.DATE_WEIGHT, "agree_bonus": C.AGREE_BONUS,
                             "agree_cap": C.AGREE_CAP}, "evidence tables changed: refit"
    assert doc["corpus"] == "G1+G3" and "G3" in doc["corpus_note"]      # chunk 7b: refit on G1 plus G3
    assert set(doc["fields"]) == set(C.CALIBRATED_FIELDS)
    assert doc["method"]["target"] == 0.005 and doc["method"]["folds"] == 5
    for name, m in doc["fields"].items():
        ps = [b["p"] for b in m["bins"]]
        assert ps == sorted(ps), name
        for pop, r in m["held_out"].items():
            assert r["rate"] <= doc["method"]["target"], (name, pop)


def test_an_artist_tag_every_track_agrees_on_is_confident_under_the_shipped_thresholds():
    # TAGS_SCORE['artist'] is not measurable on G1; it has to clear whatever G1 fitted.
    a = C.assess("artist", {"k": C.TAGS_SCORE["artist"]}, "k")
    assert a.confidence == "confident"


def test_a_full_date_needs_two_sources_to_be_confident():
    both = C.DATE_WEIGHT["info"] + C.DATE_WEIGHT["folder"] + C.AGREE_BONUS["date"]
    assert C.assess("date", {(1979, 6, 14): both}, (1979, 6, 14)).confidence == "confident"
    assert C.assess("date", {(1979, 6, 14): C.DATE_WEIGHT["info"]}, (1979, 6, 14)).confidence == "tentative"
    assert C.assess("date", {(1979, 6, 14): C.DATE_WEIGHT["tags"]}, (1979, 6, 14)).confidence == "confident"


# ── the verdict ──────────────────────────────────────────────────────────────

def _resolved(artist=("Test Band", "confident"), date=((1979, 6, 14), "confident"), kind="live",
              duplicates=()):
    def f(value, conf, **kw):
        return Field(value=value, source="info", confidence=conf, **kw)
    d = None if date[0] is None else {"year": date[0][0], "month": date[0][1], "day": date[0][2]}
    e = Field()
    return Resolved(date=f(d, date[1]) if d else Field(), artist=f(artist[0], artist[1]) if artist[0] else Field(),
                    venue=e, event=e, stage=e, city=e, state=e, country=e, source=e, lineage=e,
                    source_tag=e, shnid=e, album=e, kind=kind, tracks=[], duplicates=list(duplicates))


def test_verdict_confident_artist_and_full_date_ingest():
    assert verdict(_resolved()) == ("ingested", [])


def test_verdict_tentative_artist():
    assert verdict(_resolved(artist=("Test Band", "tentative"))) == ("review", ["tentative:artist"])


def test_verdict_tentative_full_date():
    assert verdict(_resolved(date=((1979, 6, 14), "tentative"))) == ("review", ["tentative:date"])


def test_verdict_both_tentative():
    st, why = verdict(_resolved(artist=("X", "tentative"), date=((1979, 6, 14), "tentative")))
    assert st == "review" and why == ["tentative:artist", "tentative:date"]


def test_verdict_partial_dates_keep_their_own_reasons_not_tentative():
    assert verdict(_resolved(date=((1979, None, None), "tentative"))) == ("review", ["needs_month"])
    assert verdict(_resolved(date=((1979, 6, None), "tentative"))) == ("review", ["needs_day"])


def test_verdict_studio_needs_no_date_and_a_tentative_date_does_not_matter():
    assert verdict(_resolved(kind="studio", date=((1979, 6, 14), "tentative"))) == ("ingested", [])
    assert verdict(_resolved(kind="studio", date=(None, "empty"))) == ("ingested", [])
    assert verdict(_resolved(kind="studio", artist=("X", "tentative"), date=(None, "empty"))) == \
        ("review", ["tentative:artist"])


def test_verdict_no_artist_is_needs_artist_not_tentative():
    assert verdict(_resolved(artist=(None, "empty"))) == ("review", ["needs_artist"])


def test_verdict_conflicts_and_duplicates_unchanged():
    from app.utils.resolve import Duplicate
    r = _resolved()
    r.date.conflict = True
    assert verdict(r) == ("review", ["conflict:date"])
    r = _resolved(artist=("X", "tentative"), duplicates=[Duplicate(kind="content", recording_id=1)])
    assert verdict(r) == ("skipped", ["duplicate_content"])


def test_a_value_a_person_set_is_locked_confident_and_clears_tentative():
    from app.api.ingest import apply_blanket_values
    r = _resolved(artist=("Guess", "tentative"))
    apply_blanket_values(r, None, {"artist": "Typed Band"})
    assert r.artist.value == "Typed Band" and r.artist.source == "applied"
    assert r.artist.confidence == "confident"
    assert verdict(r) == ("ingested", [])


def test_an_unassessed_field_is_confident_with_a_value_and_empty_without():
    assert Field(value="x").confidence == "confident"
    assert Field().confidence == "empty"


# ── resolve(): evidence and confidence on hand-built scans ───────────────────

def _scan(info=None, tags=None, folder="", evidence=None):
    fi = {"artist": None, "year": None, "month": None, "day": None, "tracks": []}
    fi.update(info or {})
    if evidence is not None:
        fi["evidence"] = evidence
    ft = {"tracks": []}
    ft.update(tags or {})
    return {"folder_name": folder, "audio_files": [], "suggestions": {"from_tags": ft, "from_info_file": fi}}


def test_scan_without_evidence_still_resolves_and_is_tentative():
    r = resolve(_scan(info={"artist": "Test Band", "year": 1979, "month": 6, "day": 14}))
    assert r.artist.value == "Test Band" and r.date.value == {"year": 1979, "month": 6, "day": 14}
    assert r.artist.confidence == "tentative" and r.date.confidence == "tentative"
    assert verdict(r)[1] == ["tentative:artist", "tentative:date"]


def test_info_and_folder_agreeing_on_a_full_date_is_confident_with_both_as_evidence():
    r = resolve(_scan(info={"artist": "Test Band", "year": 1979, "month": 6, "day": 14},
                      folder="Test Band - 1979-06-14 - Stars - Philadelphia, PA (SBD)"))
    assert r.date.confidence == "confident"
    assert [e["source"] for e in r.date.evidence] == ["info", "folder"]
    assert {e["source"] for e in r.artist.evidence} == {"info", "folder"}   # the template folder agrees


def test_a_year_only_info_date_does_not_make_a_folders_full_date_confident():
    r = resolve(_scan(info={"artist": "X", "year": 1971},
                      folder="X - 1971-01-01 - Unknown Venue - Berkeley, CA (SBD)"))
    assert r.date.value == {"year": 1971, "month": 1, "day": 1}
    assert r.date.confidence == "tentative"


def test_tags_artist_all_tracks_agree_is_confident_and_a_disagreement_is_a_conflict():
    tracks = [{"raw": {"artist": "Test Band"}}, {"raw": {"artist": "Test Band"}}]
    r = resolve(_scan(tags={"tracks": tracks}))
    assert r.artist.value == "Test Band" and r.artist.confidence == "confident"
    assert r.artist.evidence[0]["source"] == "tags"
    r = resolve(_scan(info={"artist": "Other Band"}, tags={"tracks": tracks}))
    assert r.artist.conflict is True and verdict(r)[0] == "review"


def test_runner_up_is_kept_only_when_close_and_never_none():
    ev = {"fields": {}, "cands": {"artist": [
        {"text": "Alpha Band", "line": 0, "score": 8.0, "role": "ARTIST", "ext": [], "runner_up": {}, "chosen": True},
        {"text": "Beta Band", "line": 4, "score": 7.0, "role": "ARTIST", "ext": [], "runner_up": {}, "chosen": False}]}}
    r = resolve(_scan(info={"artist": "Alpha Band"}, evidence=ev))
    assert r.artist.runner_up["value"] == "Beta Band" and r.artist.runner_up["line"] == 4
    assert r.artist.confidence == "tentative"
    ev["cands"]["artist"][1]["score"] = -5.0
    assert resolve(_scan(info={"artist": "Alpha Band"}, evidence=ev)).artist.runner_up is None


def test_a_value_whose_score_does_not_beat_none_is_dropped():
    ev = {"fields": {}, "cands": {"venue": [
        {"text": "Maybe Hall", "line": 2, "score": -1.0, "role": "VENUE", "ext": [], "runner_up": {}, "chosen": True}]}}
    r = resolve(_scan(info={"venue": "Maybe Hall"}, evidence=ev))
    assert r.venue.value is None and r.venue.confidence == "empty"


# ── resolver_json round trip ─────────────────────────────────────────────────

def test_resolver_json_round_trips_evidence_and_confidence():
    from app.api.ingest import resolver_json_for_storage
    r = resolve(_scan(info={"artist": "Test Band", "year": 1979, "month": 6, "day": 14},
                      folder="Test Band - 1979-06-14 - Stars - Philadelphia, PA (SBD)"))
    stored = json.loads(resolver_json_for_storage(r.to_dict()))
    for name in ("date", "artist", "venue", "city", "state", "country"):
        f = stored[name]
        assert {"confidence", "evidence", "runner_up", "p", "margin", "logit"} <= set(f)
    assert stored["date"]["confidence"] == "confident"
    assert stored["date"]["evidence"] == r.date.to_dict()["evidence"]
    assert set(stored["date"]["evidence"][0]) == {"source", "text", "line", "role", "extractor", "score", "notes"}
    assert "tracks" not in stored


def test_an_older_resolver_json_without_the_new_keys_is_still_a_plain_dict():
    from app.api.ingest import resolver_json_for_storage
    old = {"date": {"value": {"year": 1979}, "source": "tags", "candidates": {}, "conflict": True},
           "kind": "live", "tracks": []}
    assert json.loads(resolver_json_for_storage(old))["date"]["conflict"] is True


# ── 2026-10-04: variants, agreement, folder dates, parent artist folder ──────

def test_ensemble_and_billing_variants_are_not_wrong():
    from app.utils import resolver_eval as ev
    for gold, pred in [("Miles Davis", "Miles Davis Quintet"), ("The Dave Matthews Band", "Dave Matthews"),
                       ("Bob Weir and Friends", "Bob Weir"), ("Phil Lesh with Warren Haynes", "Phil Lesh"),
                       ("Bill Evans Trio", "Bill Evans Quartet")]:
        assert ev.judge_field("artist", gold, pred) == "variant", (gold, pred)
        assert not ev.is_wrong("artist", "variant")
    # other containment stays partial or wrong
    assert ev.judge_field("artist", "Bela Fleck", "Bela Fleck & Sandip Burman") == "partial"
    assert ev.judge_field("artist", "Grateful Dead", "Dead & Company") == "wrong"
    assert ev.judge_field("artist", "Phish", "phish") == "right"


def _artist_scan(info=None, tags=None, folder_name=""):
    return {"folder_path": None, "folder_name": folder_name, "audio_file_count": 0, "audio_files": [],
            "suggestions": {"from_tags": {"tracks": [], **({"artist": tags} if tags else {})},
                            "from_info_file": {"artist": info} if info else {}}}


def test_agreement_raises_the_score_of_a_value_two_sources_state():
    one = resolve(_artist_scan(info="Phish"))
    two = resolve(_artist_scan(info="Phish", folder_name="Phish - 1997-11-22 - Hampton Coliseum - Hampton, VA"))
    assert two.artist.logit > one.artist.logit
    assert "agreed by" in " ".join(r["notes"] for r in two.artist.evidence)
    assert "agreed by" not in " ".join(r["notes"] for r in one.artist.evidence)


def test_agreement_needs_independent_sources_not_the_same_one_twice():
    from app.utils.resolve import _Groups, _row
    g = _Groups("artist")
    g.add("x", 5.0, "X", [_row("info", "X")])
    g.add("x", 5.0, "X", [_row("info", "X")])
    assert g.scores()["x"] == 10.0
    g.add("x", 5.0, "X", [_row("tags", "X")])
    assert g.scores()["x"] == 15.0 + C.AGREE_BONUS["artist"]
    g.add("x", 1.0, "X", [_row("library", "X", extractor="library")])
    assert g.scores()["x"] == 16.0 + C.AGREE_BONUS["artist"]


@pytest.mark.parametrize("name", ["Phish 1997-11-22 Hampton", "phish97-11-22.sbd", "Phish 11-22-97",
                                  "Phish Nov 22 1997 Hampton", "Phish - 1997.11.22 - Hampton"])
def test_any_full_date_in_the_folder_name_is_a_date_candidate(name):
    from app.utils.resolve import _date_from_folder
    assert _date_from_folder(name) == (1997, 11, 22)


@pytest.mark.parametrize("name", ["Phish 1997", "Phish 1997-11", "Phish 1997-11-22 (transferred 2015-03-02)",
                                  "Phish 1997-11-22 and 1997-11-23"])
def test_a_folder_name_without_one_clear_full_date_gives_none_or_the_show(name):
    from app.utils.resolve import _date_from_folder
    got = _date_from_folder(name)
    assert got in (None, (1997, 11, 22)) and not (name.endswith("11-23") and got)


def test_parent_artist_folder_is_a_candidate_with_library_placement(tmp_path):
    lib = tmp_path / "lib"
    show = lib / "Phish" / "1997" / "Phish 1997-11-22"
    show.mkdir(parents=True)
    scan = _artist_scan(folder_name=show.name)
    scan["folder_path"] = str(show)
    res = resolve(scan, library_root=str(lib), placement="artist")
    assert res.artist.value == "Phish" and res.artist.source == "folder"
    assert any(r["extractor"] == "folder tree" for r in res.artist.evidence)
    scan["suggestions"]["from_info_file"] = {"artist": "Phish"}
    both = resolve(scan, library_root=str(lib), placement="artist")
    assert both.artist.logit > res.artist.logit


# ── fail-safe calibration ────────────────────────────────────────────────────

@pytest.fixture
def broken_cal():
    yield
    C.set_calibration(None)
    C.load_calibration(force=True)


def _all_tentative_and_review(caplog):
    C.set_calibration(None)
    scan = _artist_scan(info="Phish", tags="Phish",
                        folder_name="Phish - 1997-11-22 - Hampton Coliseum - Hampton, VA")
    scan["suggestions"]["from_tags"]["concert_date"] = "1997-11-22"
    scan["suggestions"]["from_info_file"].update({"year": 1997, "month": 11, "day": 22})
    res = resolve(scan)
    for f in C.CALIBRATED_FIELDS:
        assert getattr(res, f).confidence in ("tentative", "empty"), f
    status, reasons = verdict(res)
    assert status == "review" and "tentative:artist" in reasons
    assert C.assess("artist", {"x": 40.0}, "x").confidence == "tentative"


@pytest.mark.parametrize("content", [None, "", "{not json", "[1, 2]", "null", '{"fields": []}',
                                     '{"fields": {"artist": {"m": 1.0, "bins": []}}}',
                                     '{"fields": {"artist": {"tau": "x", "m": 1.0}}}',
                                     '{"fields": {"artist": {"tau": 0.5, "m": 1.0, "bins": [3]}}}'])
def test_unusable_calibration_file_makes_every_field_tentative(tmp_path, monkeypatch, broken_cal, caplog, content):
    p = tmp_path / "calibration.json"
    if content is not None:
        p.write_text(content, encoding="utf-8")
    monkeypatch.setattr(C, "CALIBRATION_PATH", p)
    C._warned.clear()
    C.set_calibration(None)
    C.load_calibration(force=True)
    with caplog.at_level("WARNING"):
        _all_tentative_and_review(caplog)
    assert any("unusable" in r.message for r in caplog.records)


def test_the_warning_is_logged_once(tmp_path, monkeypatch, broken_cal, caplog):
    monkeypatch.setattr(C, "CALIBRATION_PATH", tmp_path / "missing.json")
    C._warned.clear()
    C.set_calibration(None)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            C.assess("artist", {"x": 40.0}, "x")
            C.assess("date", {(1997, 11, 22): 40.0}, (1997, 11, 22))
    assert sum("artist" in r.message or "missing" in r.message for r in caplog.records) <= 2
    assert len([r for r in caplog.records if "no valid model for artist" in r.message]) == 1


def test_set_calibration_with_wrong_shape_is_failsafe(broken_cal):
    C.set_calibration([1, 2])
    assert C.assess("artist", {"x": 40.0}, "x").confidence == "tentative"


# ── free-form folder name as a second artist source ─────────────────────────

def _src_names(r):
    return {e["source"] for e in r.artist.evidence}


@pytest.mark.parametrize("folder", [
    "Boxcars July 25, 2014 Bicentennial Park Pavilion Columbus, Ohio",     # prefix
    "Keith Jarrett - 1976-02-17 Milwaukee; American Quartet",               # prefix, hyphen
    "1976-02-17 Milwaukee The Boxcars live",                                # contained, leading The
])
def test_free_form_folder_naming_the_artist_is_a_folder_source(folder):
    name = "Keith Jarrett" if "Jarrett" in folder else "Boxcars"
    r = resolve(_artist_scan(info=name, folder_name=folder))
    assert _src_names(r) == {"info", "folder"}


def test_folder_match_is_on_word_boundaries():
    r = resolve(_artist_scan(info="Phish", folder_name="Phishing Expedition 1997-11-22"))
    bare = resolve(_artist_scan(info="Phish"))
    assert r.artist.logit == bare.artist.logit                  # not corroborated: the lead is only shown
    assert "agreed by" not in " ".join(e["notes"] for e in r.artist.evidence)


@pytest.mark.parametrize("info", ["Tim", "Yes", "UB4", "X"])
def test_short_names_never_count(info):
    r = resolve(_artist_scan(info=info, folder_name=f"{info} 1997-11-22 Hampton Coliseum"))
    assert _src_names(r) == {"info"}


def test_a_mismatched_free_form_folder_is_neutral_and_does_not_agree():
    r = resolve(_artist_scan(info="S U R V I V O R S' S U I T E Concerts",
                             folder_name="Keith Jarrett - 1976-02-17 Milwaukee; American Quartet"))
    bare = resolve(_artist_scan(info="S U R V I V O R S' S U I T E Concerts"))
    assert r.artist.logit == bare.artist.logit                   # shown in the popover, scores nothing
    assert "agreed by" not in " ".join(e["notes"] for e in r.artist.evidence)
    assert not r.artist.conflict


def test_a_date_only_folder_is_neutral():
    r = resolve(_artist_scan(info="Phish", folder_name="1997-11-22"))
    assert _src_names(r) == {"info"}
    assert not r.artist.conflict


def test_free_form_agreement_adds_the_bonus_not_a_score():
    base = resolve(_artist_scan(info="Phish", folder_name="1997-11-22"))
    agree = resolve(_artist_scan(info="Phish", folder_name="Phish 1997-11-22 Hampton"))
    assert agree.artist.logit > base.artist.logit
    assert agree.artist.margin > base.artist.margin


# ── the folder lead: strong agreement and the billing extension ──────────────

@pytest.mark.parametrize("name,lead", [
    ("Ella Fitzgerald & Joe Pass - 1984-05-03 - Teatro Tenda Lampugnano - Milano, Italy", "Ella Fitzgerald & Joe Pass"),
    ("Bela Fleck & Friends - 2004-04-30 - Merlefest Cabin Stage - Wilkesboro, NC (XM A)", "Bela Fleck & Friends"),
    ("Bill Frisell, Jerry Douglas, Victor Krauss - 1997-10-25 - St. Ann's Church", "Bill Frisell, Jerry Douglas, Victor Krauss"),
    ("Darol Anger's Fiddle Congress and Melee - 2001-04-01 - Freight & Salvage", "Darol Anger's Fiddle Congress and Melee"),
    ("J.D. Crowe and the New South - 1999-02-27 - Wintergrass Festival", "J.D. Crowe and the New South"),
    ("Mark O'Connor, Edgar Meyer, Yo Yo Ma - 1996-11-06 - Bottom Line", "Mark O'Connor, Edgar Meyer, Yo Yo Ma"),
    ("Merlefest Midnight Jam - 1991-04-28 - Walker Center", "Merlefest Midnight Jam"),
    ("Miles Davis & John Coltrane - 1960-04-09 - Green Dolphin Street", "Miles Davis & John Coltrane"),
    ("Pat Metheny Secret Story; 1992-11-04; Landmark Theatre; Syracuse, NY rm", "Pat Metheny Secret Story"),
    ("Trio! (Bela Fleck, Stanley Clarke, Jean-Luc Ponty) - 2005-06-21", "Trio!"),
    ("Keith Jarrett Quartet - 1976-09-18 - Unknown Venue - Frankfurt, Germany (FM)", "Keith Jarrett Quartet"),
    ("Fela Kuti - 1986-11-15 - Olympic Auditorium - Los Angeles, CA", "Fela Kuti"),
    ("Boxcars July 25, 2014 Bicentennial Park Pavilion Columbus, Ohio", "Boxcars"),
    ("GoGo Penguin - Jazz Middelheim - Antwerp 2025-06-09", "GoGo Penguin"),
    ("Herbie Hancock 1984-09-10 Blossom Music Center", "Herbie Hancock"),
    ("Keith Jarrett - 1976-02-17 Milwaukee; American Quartet", "Keith Jarrett"),
    ("Miles Davis Hempstead NY 1975 March 22 remaster", "Miles Davis Hempstead NY"),
    ("oscar peterson 1970-11-19 cologne", "oscar peterson"),
    ("Shakti; 1976-06-28; Wollman Rink Central Park; New York, N.Y", "Shakti"),
    ("1976-02-17", None),
    ("", None),
])
def test_folder_lead(name, lead):
    from app.utils.reader.folder import folder_lead
    assert folder_lead(name) == lead


def test_any_folder_name_naming_the_artist_is_a_full_source():
    # Ryan, 2026-10-06: any naming scheme, not only Trellis's or artist-first.
    lead = resolve(_artist_scan(info="Boxcars", folder_name="Boxcars July 25, 2014 Bicentennial Park Pavilion"))
    inside = resolve(_artist_scan(info="Boxcars", folder_name="2014-07-25 Bicentennial Park Boxcars"))
    billing = resolve(_artist_scan(info="Tony Allen",
                                   folder_name="Tony Allen _ Nathalie Natiembé & Bumcello _ Le Pont 2009.10.28"))
    base = resolve(_artist_scan(info="Boxcars", folder_name="2014-07-25"))
    for r in (lead, inside):
        assert r.artist.logit - base.artist.logit >= C.FOLDER_TEMPLATE_SCORE["artist"]
    assert billing.artist.value == "Tony Allen"
    assert any(e["source"] == "folder" and e["score"] == C.FOLDER_TEMPLATE_SCORE["artist"]
               for e in billing.artist.evidence)


def test_a_longer_lead_is_not_a_strong_agreement(monkeypatch):
    import app.utils.resolve as R
    monkeypatch.setattr(R, "FOLDER_LEAD_EXTENDS", False)
    r = resolve(_artist_scan(info="Pat Metheny", folder_name="Pat Metheny Group 1990-06-23 Mellon Hall"))
    assert r.artist.value == "Pat Metheny"
    assert r.artist.p is not None and r.artist.confidence != "confident"


def test_the_lead_must_be_the_whole_reading_not_a_city():
    r = resolve(_artist_scan(info="Los Angeles", folder_name="Fela Kuti - 1986-11-15 - Olympic Auditorium - Los Angeles, CA"))
    assert "folder" not in {e["source"] for e in r.artist.evidence} or r.artist.confidence != "confident"


@pytest.mark.parametrize("info,folder,expect", [
    ("Ella Fitzgerald", "Ella Fitzgerald & Joe Pass - 1984-05-03 - Teatro Tenda", "Ella Fitzgerald & Joe Pass"),
    ("Pat Metheny", "Pat Metheny, Herbie Hancock, Dave Holland - 1990-06-23 - Mellon Hall", "Pat Metheny, Herbie Hancock, Dave Holland"),
    ("Keith Jarrett", "Keith Jarrett Quartet - 1976-09-18 - Frankfurt", "Keith Jarrett Quartet"),
    ("J.D. Crowe", "J.D. Crowe and the New South - 1999-02-27 - Wintergrass", "J.D. Crowe And The New South"),
])
def test_a_well_formed_billing_in_the_folder_is_proposed_but_stays_tentative(info, folder, expect):
    r = resolve(_artist_scan(info=info, folder_name=folder))
    assert r.artist.value.lower() == expect.lower()
    assert r.artist.confidence == "tentative"


@pytest.mark.parametrize("info,folder", [
    ("Pat Metheny", "Pat Metheny Secret Story - 1992-11-04 - Landmark Theatre"),
    ("Keith Jarrett", "Keith Jarrett - 1976-02-17 Milwaukee; American Quartet"),
    ("Phish", "Phish Hampton 1997-11-22"),
])
def test_a_lead_that_continues_as_something_else_is_not_proposed(info, folder):
    assert resolve(_artist_scan(info=info, folder_name=folder)).artist.value == info


def test_the_extension_switch_turns_the_proposal_off(monkeypatch):
    import app.utils.resolve as R
    monkeypatch.setattr(R, "FOLDER_LEAD_EXTENDS", False)
    r = resolve(_artist_scan(info="Ella Fitzgerald", folder_name="Ella Fitzgerald & Joe Pass - 1984-05-03 - Teatro"))
    assert r.artist.value == "Ella Fitzgerald"


# ── reviewer fixes: which leads may agree, which extensions are proposed ─────

def _bonus_only(info, folder):
    base = resolve(_artist_scan(info=info, folder_name="1997-11-22"))
    r = resolve(_artist_scan(info=info, folder_name=folder))
    return r.artist.logit - base.artist.logit


@pytest.mark.parametrize("info,folder", [
    ("Various Artists", "Various Artists - 1997-11-22 - Hampton"),
    ("Soundcheck", "Soundcheck 1997-11-22 Hampton Coliseum"),
    ("Concert", "Concert - 1997-11-22"),
    ("Master", "Master 1997-11-22"),
    ("Bootleg", "Bootleg - 1997-11-22"),
    ("SBD", "SBD 1997-11-22"),
    ("Newport Jazz Festival", "Newport Jazz Festival - 1997-08-10 - Fort Adams"),
])
def test_a_generic_or_festival_lead_earns_nothing(info, folder):
    assert _bonus_only(info, folder) == 0


@pytest.mark.parametrize("info,folder", [
    ("ronnie", "ronnie2015-05-01.picklemix"),
    ("Alligators", "Alligators2023-03-17"),
    ("cracker", "cracker2017-12-29"),
])
def test_a_shortcode_glued_to_digits_never_gets_the_full_score(info, folder):
    assert _bonus_only(info, folder) < C.FOLDER_TEMPLATE_SCORE["artist"]


def test_a_known_venue_lead_earns_nothing():
    from app.utils.reader.library import LibraryIndex
    from app.utils.resolve import _free_form_folder_source, _Groups
    lib = LibraryIndex.from_dicts(venues=["Hampton Coliseum"])
    g = _Groups("artist")
    _free_form_folder_source(g, "Hampton Coliseum", "Hampton Coliseum", "Hampton Coliseum - 1997-11-22", library=lib, atlas=None)
    assert g.d == {}


def test_an_atlas_place_lead_earns_nothing():
    from types import SimpleNamespace
    from app.utils.resolve import _free_form_folder_source, _Groups

    class A:
        def venue(self, *a, **k): return []
        def event(self, *a, **k): return []
        def area(self, text, **k): return [SimpleNamespace()] if text == "Boston" else []
    g = _Groups("artist")
    _free_form_folder_source(g, "Boston", "Boston", "Boston - 1997-11-22", library=None, atlas=A())
    assert g.d == {}


@pytest.mark.parametrize("folder", [
    "Miles Davis, Chicago 1975-02-01",
    "Miles Davis and Chicago 1975-02-01",
    "Miles Davis, 1975-02-01",
    "Miles Davis & Newport Jazz Festival 1975-07-04",
    "Miles Davis, Chicago Theatre - 1975-02-01",
])
def test_an_extension_that_is_not_a_person_or_act_is_not_proposed(folder):
    assert resolve(_artist_scan(info="Miles Davis", folder_name=folder)).artist.value == "Miles Davis"


def test_an_extension_naming_a_known_place_is_not_proposed():
    from types import SimpleNamespace
    from app.utils.resolve import _free_form_folder_source, _Groups

    class A:
        def venue(self, *a, **k): return []
        def event(self, *a, **k): return []
        def area(self, text, **k): return [SimpleNamespace()] if text == "New York" else []
    out = _free_form_folder_source(_Groups("artist"), "Miles Davis", "Miles Davis",
                                   "Miles Davis, New York - 1975-02-01", library=None, atlas=A())
    assert out is None


# ── The folder lead as a variant of the reading, and as display evidence ────────────

@pytest.mark.parametrize("info,folder,shown", [
    ("Miles Davis Septet", "Miles Davis - 1971-10-26 - Brussels, Belgium", "Miles Davis"),
    ("Oscar Peterson Trio", "oscar peterson 1970-11-19 cologne", "Oscar Peterson"),
    ("Count Basie Big Band", "Count Basie 1970-05-01 Paris", "Count Basie"),
])
def test_a_lead_that_is_the_reading_without_its_group_word_agrees(info, folder, shown):
    from app.utils.sources_plain import with_sources_plain
    plain = resolve(_artist_scan(info=info, folder_name=folder))
    bare = resolve(_artist_scan(info=info))
    assert plain.artist.value == info and plain.artist.source == "info"      # the reading still wins
    assert plain.artist.logit > bare.artist.logit
    assert plain.artist.conflict is False
    assert with_sources_plain(plain.to_dict())["sources_plain"]["artist"]["folder"] == shown


def test_a_variant_lead_still_obeys_the_guards():
    # a generic word and a festival never agree, however the reading is spelled
    for info, folder in [("Live Band", "Live - 1997-11-22 - Somewhere"),
                         ("Newport Folk Festival Band", "Newport Folk Festival - 1965-07-25")]:
        r = resolve(_artist_scan(info=info, folder_name=folder))
        assert r.artist.logit == resolve(_artist_scan(info=info)).artist.logit
    # a shortcode glued to the date gets no more than the agreement bonus
    glued = resolve(_artist_scan(info="Ronnie Band", folder_name="ronnie2015-05-01"))
    assert glued.artist.logit <= resolve(_artist_scan(info="Ronnie Band")).artist.logit + C.AGREE_BONUS["artist"] + 0.01


def test_a_disagreeing_act_lead_is_shown_and_scores_nothing():
    from app.utils.sources_plain import with_sources_plain
    bare = resolve(_artist_scan(info="Phish"))
    r = resolve(_artist_scan(info="Phish", folder_name="Grateful Dead - 1977-05-08 - Barton Hall"))
    assert r.artist.value == "Phish"
    assert r.artist.logit == bare.artist.logit and r.artist.margin == bare.artist.margin
    assert r.artist.conflict is False and r.artist.confidence == bare.artist.confidence
    assert with_sources_plain(r.to_dict())["sources_plain"]["artist"]["folder"] == "Grateful Dead"
    row = [e for e in r.artist.evidence if e["source"] == "folder"]
    assert row and row[0]["text"] == "Grateful Dead" and not row[0]["score"]


@pytest.mark.parametrize("folder", [
    "Live - 1997-11-22 - Hampton",
    "Newport Folk Festival - 1965-07-25",
    "ronnie2015-05-01",
])
def test_a_lead_that_is_not_an_act_shows_nothing(folder):
    from app.utils.sources_plain import with_sources_plain
    r = resolve(_artist_scan(info="Phish", folder_name=folder))
    assert with_sources_plain(r.to_dict())["sources_plain"]["artist"]["folder"] is None
    assert not [e for e in r.artist.evidence if e["source"] == "folder"]


# ── Cross-checks between the sources (2026-10-06) ───────────────────────────────────

class _ActAtlas:
    """An Atlas that knows some acts (exactly) and, optionally, which shows an act played."""

    def __init__(self, acts=(), shows=None, place_keys=None):
        from types import SimpleNamespace
        self._ns = SimpleNamespace
        self.acts = {x.lower() for x in acts}
        self.shows = shows or {}
        self._pk = place_keys or {}

    def artist(self, text, fuzzy=True, limit=5, **kw):
        if str(text).lower() in self.acts:
            return [self._ns(name=text, how="exact", extra={"popularity": 50})]
        return []

    def venue(self, *a, **k): return []
    def event(self, *a, **k): return []
    def area(self, *a, **k): return []
    def musician(self, *a, **k): return []

    def event_place(self, artist, date):
        return list(self.shows.get((str(artist).lower(), tuple(date)), []))

    def place_keys(self, pid):
        return list(self._pk.get(pid, []))


@pytest.fixture
def acts(monkeypatch):
    """Install an Atlas (and an empty library) for resolve()'s lookups; returns a setter."""
    from app.utils.reader.library import LibraryIndex

    def install(atlas):
        monkeypatch.setattr("app.atlas.lookup.current_atlas", lambda: atlas)
        monkeypatch.setattr("app.utils.reader.library.current_library", lambda: LibraryIndex.empty())
        return atlas
    install(_ActAtlas())
    return install


def _scan_with(info=None, folder_name="", tags=None, date=None, venue=None, city=None):
    s = _artist_scan(info=info, tags=tags, folder_name=folder_name)
    if tags:
        s["suggestions"]["from_tags"]["tracks"] = [
            {"index": i, "filename": f"{i:02d}.flac", "raw": {"artist": tags}} for i in (1, 2)]
    fi = s["suggestions"]["from_info_file"]
    if date:
        fi.update({"year": date[0], "month": date[1], "day": date[2]})
    if venue:
        fi["venue"] = venue
    if city:
        fi["city"] = city
    return s


KJ_FOLDER = "Keith Jarrett - 1976-02-17 Milwaukee; American Quartet"
KJ_INFO = "SURVIVORS' SUITE Concerts"


def test_a_known_act_in_the_folder_beats_a_reading_that_matches_no_act(acts):
    acts(_ActAtlas(acts=["Keith Jarrett"]))
    r = resolve(_scan_with(info=KJ_INFO, folder_name=KJ_FOLDER))
    assert r.artist.value == "Keith Jarrett" and r.artist.source == "folder"
    assert r.artist.confidence == "tentative"
    assert r.artist.candidates["info"] == KJ_INFO and r.artist.candidates["folder"] == "Keith Jarrett"
    assert "the folder names a known act" in " ".join(e["notes"] for e in r.artist.evidence)


def test_the_folder_does_not_win_when_both_readings_are_known_acts_or_neither_is(acts):
    acts(_ActAtlas(acts=["Keith Jarrett", "Phish"]))
    both = resolve(_scan_with(info="Phish", folder_name=KJ_FOLDER))
    assert both.artist.value == "Phish" and both.artist.source == "info"
    acts(_ActAtlas())
    neither = resolve(_scan_with(info=KJ_INFO, folder_name=KJ_FOLDER))
    assert neither.artist.value == KJ_INFO and neither.artist.source == "info"


def test_a_lead_that_is_not_an_act_does_not_win_even_when_the_atlas_knows_the_name(acts):
    acts(_ActAtlas(acts=["Live"]))                       # a generic word is never a lead
    r = resolve(_scan_with(info=KJ_INFO, folder_name="Live - 1976-02-17 Milwaukee"))
    assert r.artist.value == KJ_INFO


def test_the_folder_act_does_not_override_tags(acts):
    acts(_ActAtlas(acts=["Keith Jarrett"]))
    r = resolve(_scan_with(info=KJ_INFO, tags="Dewey Redman", folder_name=KJ_FOLDER))
    assert r.artist.value == "Dewey Redman" and r.artist.source == "tags"


def test_a_billing_that_holds_the_lead_after_other_names_is_kept(acts):
    acts(_ActAtlas(acts=["Warne Marsh"]))
    r = resolve(_scan_with(info="Bill Evans Trio With Warne Marsh",
                           folder_name="Warne Marsh - Bill Evans trio - San Francisco 1977"))
    assert r.artist.value == "Bill Evans Trio With Warne Marsh"


# the Atlas lists the act on this very day

SHOW = {"place_id": 7, "place": "Allegany County Fairgrounds", "city": "Cumberland", "event": None,
        "kind": None, "date": "2003-06-15", "act": "Del McCoury Band", "exact": True}


def test_the_show_index_is_an_independent_artist_source(acts):
    acts(_ActAtlas(shows={("del mccoury band", (2003, 6, 15)): [SHOW]}))
    on = resolve(_scan_with(info="Del McCoury Band", date=(2003, 6, 15)))
    off = resolve(_scan_with(info="Del McCoury Band", date=(2003, 6, 16)))
    assert on.artist.logit == off.artist.logit + C.ATLAS_SHOW_SCORE["artist"] + C.AGREE_BONUS["artist"]
    rows = [e for e in on.artist.evidence if e["extractor"] == "show index"]
    assert rows and rows[0]["source"] == "atlas"
    assert "agreed by atlas, info" in " ".join(e["notes"] for e in on.artist.evidence)
    assert not [e for e in off.artist.evidence if e["extractor"] == "show index"]


def test_the_show_index_needs_an_exact_day_and_the_act_on_it(acts):
    acts(_ActAtlas(shows={("del mccoury band", (2003, 6, 15)): [SHOW]}))
    for date, info in [((2003, 6, None), "Del McCoury Band"), ((2003, 6, 15), "Doc Watson")]:
        r = resolve(_scan_with(info=info, date=date))
        assert not [e for e in r.artist.evidence if e["extractor"] == "show index"]


def test_the_show_index_corroborates_the_venue_and_city_where_the_place_matches(acts):
    acts(_ActAtlas(shows={("del mccoury band", (2003, 6, 15)): [SHOW]},
                   place_keys={7: ["allegany county fairgrounds"]}))
    r = resolve(_scan_with(info="Del McCoury Band", date=(2003, 6, 15), venue="Allegany County Fairgrounds",
                           city="Cumberland"))
    assert [e for e in r.venue.evidence if e["extractor"] == "show index"]
    assert [e for e in r.city.evidence if e["extractor"] == "show index"]
    other = resolve(_scan_with(info="Del McCoury Band", date=(2003, 6, 15), venue="Some Other Hall", city="Elsewhere"))
    assert not [e for e in other.venue.evidence if e["extractor"] == "show index"]
    assert not [e for e in other.city.evidence if e["extractor"] == "show index"]


def test_the_show_index_alone_can_make_an_artist_confident(acts):
    acts(_ActAtlas(shows={("del mccoury band", (2003, 6, 15)): [SHOW]}))
    from app.utils.reader import confidence as conf
    scan = _scan_with(info="Del McCoury Band", date=(2003, 6, 15))
    scan["suggestions"]["from_info_file"]["evidence"] = {
        "fields": {"artist": {"text": "Del McCoury Band", "line": 0, "score": 9.0, "role": "ARTIST", "via": ""}}}
    assert resolve(scan).artist.confidence == "confident"
    assert conf.ATLAS_SHOW_SCORE["artist"] > 0


# tags, folder lead and info text: two that agree win

def test_the_info_text_and_the_folder_outvote_the_tags(acts):
    r = resolve(_scan_with(info="Grateful Dead", tags="Phil Lesh", folder_name="Grateful Dead - 1977-05-08 - Barton Hall"))
    assert r.artist.value == "Grateful Dead" and r.artist.source == "info"
    assert r.artist.conflict is True                      # the tags still disagree: a person looks


def test_the_folder_and_a_group_word_variant_of_the_info_outvote_the_tags(acts):
    r = resolve(_scan_with(info="Miles Davis Septet", tags="Wayne Shorter", folder_name="Miles Davis - 1971-10-26"))
    assert r.artist.value == "Miles Davis Septet"


def test_three_different_readings_keep_the_tags(acts):
    r = resolve(_scan_with(info="Grateful Dead", tags="Phil Lesh", folder_name="Jerry Garcia Band - 1977-05-08 - Barton Hall"))
    assert r.artist.value == "Phil Lesh" and r.artist.source == "tags" and r.artist.conflict is True


def test_tags_and_info_agreeing_beat_a_folder_that_differs(acts):
    r = resolve(_scan_with(info="Grateful Dead", tags="Grateful Dead", folder_name="Jerry Garcia Band - 1977-05-08 - Barton Hall"))
    assert r.artist.value == "Grateful Dead" and r.artist.conflict is False
    assert r.artist.candidates["folder"] == "Jerry Garcia Band"


# a known act on a personnel line is not the billing

def test_a_known_act_on_a_personnel_line_does_not_take_the_artist(acts):
    from app.utils.ingest import parse_info_file
    atlas = _ActAtlas(acts=["Keith Jarrett"])
    text = ("Scrambled Greg\nMitchell Auditorium\nMilwaukee, Wisconsin\nFebruary 17, 1976\n\n"
            "Keith Jarrett - piano\nDewey Redman - tenor sax\nCharlie Haden - bass\nPaul Motian - drums\n")
    r = parse_info_file(None, text=text, atlas=atlas)
    assert r["artist"] == "Scrambled Greg"
