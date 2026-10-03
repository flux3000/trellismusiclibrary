"""
tests/test_resolver_eval.py -- the Resolver v2 harness (app/utils/resolver_eval.py).

A smoke test on the G4 fixtures plus the judging rules. It asserts the REPORT
SHAPE and the judge, never accuracy: accuracy is what the harness measures, and
a gate lives in the meeting note, not in CI.
"""
import json

import pytest

from app.utils import resolver_eval as ev


# ── Judging ──────────────────────────────────────────────────────────────────

def test_norm_ignores_accents_case_ampersand_and_leading_the():
    assert ev.norm("Paco de Lucía") == ev.norm("PACO DE LUCIA")
    assert ev.norm("Simon & Garfunkel") == ev.norm("simon and garfunkel")
    assert ev.norm("The Birchmere") == ev.norm("Birchmere")


def test_judge_text_levels():
    assert ev.judge_text("Royal Albert Hall", "royal albert hall") == "right"
    assert ev.judge_text("Royal Albert Hall", "Royal Albert Hall, London") == "partial"
    assert ev.judge_text("Town Park", "Telluride Bluegrass Festival") == "wrong"
    assert ev.judge_text("Town Park", "") == "empty"
    assert ev.judge_text("", "anything") is None          # key has no value


def test_partial_needs_whole_words():
    assert ev.judge_text("Ark", "Park Avenue") == "wrong"


def test_gold_false_means_expected_empty():
    assert ev.judge_text(False, None) == "right"
    assert ev.judge_text(False, "Source 1: Schoeps") == "wrong"


def test_judge_date_is_component_wise():
    gold = {"y": 1983, "m": 6, "d": 18}
    assert ev.judge_date(gold, (1983, 6, 18)) == "right"
    assert ev.judge_date(gold, (1983, None, None)) == "partial"      # less precise, consistent
    assert ev.judge_date(gold, (1983, 10, 3)) == "wrong"            # month and day invented
    assert ev.judge_date(gold, (None, None, None)) == "empty"
    assert ev.judge_date({"y": 1974, "m": 8, "d": None}, (1974, 8, 3)) == "wrong"   # invented day
    assert ev.judge_date({"y": None, "m": None, "d": None}, (1974, 8, 3)) is None


def test_state_and_country_aliases():
    assert ev.judge_field("state", "PA", "Pennsylvania") == "right"
    assert ev.judge_field("country", "US", "USA") == "right"
    assert ev.judge_field("country", "UK", "United Kingdom") == "right"


# ── Smoke: the whole tier runs and the report has its shape ──────────────────

@pytest.mark.parametrize("mode", ["reader", "resolve"])
def test_g4_report_shape(mode, tmp_path):
    rep = ev.run_tier("G4", mode, corpus_dir=tmp_path)
    assert rep["tier"] == "G4" and rep["mode"] == mode
    assert rep["n"] > 0 and rep["errors"] == 0
    assert set(rep["fields"]) == set(ev.FIELDS)
    for stats in rep["fields"].values():
        for key in ("right", "partial", "wrong", "empty", "n", "no_key",
                    "precision", "recall", "wrong_rate", "confident_but_wrong"):
            assert key in stats
        assert stats["n"] == stats["right"] + stats["partial"] + stats["wrong"] + stats["empty"]
    assert {"p50_ms", "p95_ms"} <= set(rep["timing"])
    assert len(rep["items"]) == rep["n"]
    if mode == "resolve":
        assert rep["auto_ingest"]["n_with_verdict"] == rep["n"]
    assert ev.format_report(rep).startswith("G4")


def test_diff_reports_changed_verdicts(tmp_path):
    first = ev.run_tier("G4", "reader", corpus_dir=tmp_path)
    ev.save_last_run(first, tmp_path)
    second = json.loads(json.dumps(first))
    # Flip any item's verdict; G4 no longer has a wrong date to pick (2026-10-03, strict grammar).
    victim = second["items"][0]
    before = victim["judgments"]["date"]
    after = "wrong" if before == "right" else "right"
    victim["judgments"]["date"] = after
    changes = ev.diff_runs(ev.load_last_run(tmp_path)["G4:reader"], second)
    assert changes == [(victim["id"], "date", before, after, victim["pred"]["date"])]
    assert ev.diff_runs(None, second) is None
    assert ev.diff_runs(first, first) == []


def test_missing_corpus_tier_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError):
        ev.run_tier("G1", "reader", corpus_dir=tmp_path)


def test_cli_runs_on_g4(tmp_path, capsys):
    assert ev.main(["--tier", "G4", "--corpus", str(tmp_path), "--diff"]) == 0
    out = capsys.readouterr().out
    assert "G4 (reader)" in out and "diff:" in out
    assert (tmp_path / ev.LAST_RUN).exists()
