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

def test_calibration_file_matches_the_evidence_tables_and_says_g1_only():
    doc = C.load_calibration(force=True)
    assert doc["none_scores"] == C.NONE_SCORE, "evidence tables changed: refit with --calibrate --write-calibration"
    assert doc["tables"] == {"date_weight": C.DATE_WEIGHT, "agree_bonus": C.AGREE_BONUS,
                             "agree_cap": C.AGREE_CAP}, "evidence tables changed: refit"
    assert "G1 only" in doc["corpus_note"] and doc["corpus"] == "G1"
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
