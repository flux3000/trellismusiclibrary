"""
tests/test_quality.py — Listening Quality scoring, staging and promotion.

Pure logic and DB behaviour only: no audio is decoded anywhere in this file.
That is possible because feature extraction and scoring are separate modules —
scoring is a pure function over a feature dict, so it can be tested against
hand-written fixtures.  If a change to the engine ever makes these tests need a
real FLAC, that separation has been broken and THAT is the bug.
"""

import unicodedata

import pytest

from app.extensions import db as _db
from app.models.performance import Performance
from app.models.artist import Artist
from app.models.recording import Recording
from app.utils import quality_store as qs
from app.utils.quality import score_recording, GROUP_WEIGHTS


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures — feature dicts, not audio
# ═════════════════════════════════════════════════════════════════════════════
def _features(**over):
    """
    A clean, mid-range recording.  Values are real measurements taken from the
    Danny Gatton 1988-06-10 Birchmere tape (Ryan grades it an A), so the numbers
    are plausible rather than invented.
    """
    base = {
        # presence_balance_db and midrange_scoop_db are still MEASURED and still
        # shown under Advanced Metrics, but carry no weight as of v3 — they
        # correlated +0.057 / -0.016 against 113 grades. Kept in the fixture so
        # a regression that silently re-weights them is visible.
        "presence_balance_db":   2.79,
        "midrange_scoop_db":    -2.96,
        "spectral_tilt_db_oct": -4.24,
        "hf_energy_ratio_db":  -16.50,
        "mid_snr_db":           20.46,
        "crowd_snr_db":         22.30,
        "hum_ratio_db":         13.33,   # display only as of v3 (r = -0.038)
        "crest_factor_db":      18.22,
        "clipping_pct":          0.0,
        "channel_rms_min_db":  -20.07,
        "phase_correlation":     0.51,
        "dropout_count":         0.0,
        # Must track QUALITY_ANALYSIS_VERSION — bumped to "2" on 2026-07-31 when
        # the audience/room features were added. A fixture pinned to an older
        # version reads as "extracted by a previous analyser" and gets skipped
        # by rescore_stored(), which would make these tests quietly vacuous.
        "analysis_version":     "2",
        "sampled": [{"track": "Track05.flac", "duration_s": 278.7,
                     "offsets": [91.2, 167.4]}],
    }
    base.update(over)
    return base


# ═════════════════════════════════════════════════════════════════════════════
# Scoring — the pure function
# ═════════════════════════════════════════════════════════════════════════════
def test_score_recording_shape():
    out = score_recording(_features())
    assert set(out) >= {"listening_quality", "score_tone", "score_noise",
                        "score_dynamics", "technical_issues",
                        "technical_deduction", "flags", "score_version"}
    assert 0 <= out["listening_quality"] <= 100


def test_clean_recording_trips_no_technical_issues():
    """Technical Issues are insurance, not discriminators — silent when clean."""
    out = score_recording(_features())
    assert out["technical_issues"] == []
    assert out["technical_deduction"] == 0.0


def test_group_weights_sum_to_one():
    assert sum(GROUP_WEIGHTS.values()) == pytest.approx(1.0)


def test_worst_group_gates_the_score():
    """
    Groups combine GEOMETRICALLY: listening is gated by the worst problem, not
    the average of all of them.  A recording that collapses on one dimension
    must score below the arithmetic mean of its groups, or the geometric
    combination has been silently replaced with an average.
    """
    out = score_recording(_features(crest_factor_db=5.0))   # squashed dynamics
    groups = [out["score_tone"], out["score_noise"], out["score_dynamics"]]
    arithmetic = sum(groups) / 3
    assert out["listening_quality"] < arithmetic


def test_dead_channel_deducts():
    out = score_recording(_features(channel_rms_min_db=-80.0))
    names = [i["issue"] for i in out["technical_issues"]]
    assert "Dead channel" in names
    assert out["technical_deduction"] >= 30.0


def test_missing_feature_does_not_crash():
    """A feature the extractor failed to produce must degrade, not explode."""
    f = _features()
    del f["crest_factor_db"]
    out = score_recording(f)
    assert out["score_dynamics"] is None
    assert out["listening_quality"] is not None


def test_scoring_is_pure_over_features():
    """Same input, same output — no hidden state, no filesystem, no clock."""
    f = _features()
    assert score_recording(f) == score_recording(f)


# ═════════════════════════════════════════════════════════════════════════════
# v3 (2026-07-31) — dead metrics, source adjustment, band verdict
# ═════════════════════════════════════════════════════════════════════════════
def test_presence_and_scoop_do_not_affect_the_score():
    """
    The v3 headline finding.  Presence balance and midrange scoop held 60% of
    the Tone group on correlations measured at n=13/n=20; against the full 113
    graded recordings they measure +0.057 and -0.016, and cross-validate at
    -0.223 together — worse than predicting the mean.  They are measurements,
    not evidence of quality.

    Swinging both across their entire observed range must not move the score by
    even a decimal.  If this test fails, something has quietly re-weighted them
    and the Danny Gatton inversion is back.
    """
    base = score_recording(_features())
    swung = score_recording(_features(presence_balance_db=-11.4,
                                      midrange_scoop_db=-8.0))
    assert swung["listening_quality"] == base["listening_quality"]
    assert swung["score_tone"] == base["score_tone"]


def test_hum_is_display_only():
    """Hum correlates -0.038 with grade; it must not move the Noise group."""
    base = score_recording(_features())
    loud = score_recording(_features(hum_ratio_db=38.0))
    assert loud["score_noise"] == base["score_noise"]


def test_source_shifts_the_score_in_the_right_direction():
    """
    Source is the strongest single predictor we have (CV r = +0.314 alone).
    An audience tape should score below the identical measurement labelled as a
    soundboard, and a matrix above it.
    """
    f = _features()
    aud = score_recording(f, source="AUD")["listening_quality"]
    sbd = score_recording(f, source="SBD")["listening_quality"]
    mtx = score_recording(f, source="MTX")["listening_quality"]
    assert aud < sbd < mtx


def test_freetext_source_strings_normalise():
    """The DB holds values like 'FM Peterw' and 'Radio', not a clean enum."""
    f = _features()
    assert (score_recording(f, source="FM Peterw")["listening_quality"]
            == score_recording(f, source="FM")["listening_quality"])
    assert (score_recording(f, source="Radio")["listening_quality"]
            == score_recording(f, source="FM")["listening_quality"])


def test_unknown_source_is_neutral_not_a_penalty():
    """Missing metadata must not be read as a bad recording."""
    f = _features()
    assert (score_recording(f, source=None)["listening_quality"]
            == score_recording(f, source="SBD")["listening_quality"])


def test_verdict_band_is_ordered_and_covers_the_range():
    from app.utils.quality.quality_scoring import verdict_band
    assert verdict_band(100.0) == "green"
    assert verdict_band(0.0) == "red"
    assert verdict_band(None) is None
    # Monotonic: a better composite can never earn a worse band.
    order = {"red": 0, "yellow": 1, "green": 2}
    seen = [order[verdict_band(v)] for v in range(0, 101, 5)]
    assert seen == sorted(seen)


def test_predicted_grade_tracks_the_composite():
    """The band thresholds are grade points, so the map must be monotonic."""
    from app.utils.quality.quality_scoring import predicted_grade
    assert predicted_grade(None) is None
    vals = [predicted_grade(v) for v in range(0, 101, 10)]
    assert vals == sorted(vals)
    assert all(0.0 <= v <= 100.0 for v in vals)


# ═════════════════════════════════════════════════════════════════════════════
# Staging store — NFC normalisation
# ═════════════════════════════════════════════════════════════════════════════
ACCENTED = "/Volumes/music/Workshop/Paco de Lucía - 1976-02-28"


def test_norm_path_composes_and_strips_slash():
    nfd = unicodedata.normalize("NFD", ACCENTED) + "/"
    out = qs.norm_path(nfd)
    assert out == unicodedata.normalize("NFC", ACCENTED)
    assert not out.endswith("/")


def test_nfd_and_nfc_paths_resolve_to_one_row(app):
    """
    The bug this prevents: macOS hands out decomposed filenames, the DB holds
    composed ones, so "Lucía" != "Lucía" byte-for-byte and every lookup silently
    misses.  This cost an afternoon on the Guitar Trio corpus (2026-07-28).
    """
    nfd = unicodedata.normalize("NFD", ACCENTED)
    nfc = unicodedata.normalize("NFC", ACCENTED)
    assert nfd != nfc                                   # genuinely different bytes

    qs.upsert_staging(nfd, source_dir="/Volumes/music/Workshop",
                      scored=score_recording(_features()), features=_features())

    assert qs.get_staging(nfd) is not None
    assert qs.get_staging(nfc) is not None
    assert qs.get_staging(nfd).id == qs.get_staging(nfc).id

    # And re-analysing via the other form must not create a second row.
    qs.upsert_staging(nfc, scored=score_recording(_features()),
                      features=_features())
    assert len(qs.list_staging("/Volumes/music/Workshop")) == 1


# ═════════════════════════════════════════════════════════════════════════════
# Triage
# ═════════════════════════════════════════════════════════════════════════════
def _stage(path="/src/show-a", source_dir="/src", **over):
    return qs.upsert_staging(path, source_dir=source_dir,
                             scored=score_recording(_features(**over)),
                             features=_features(**over))


def test_new_analysis_starts_pending(app):
    assert _stage().triage_status == qs.TRIAGE_PENDING


def test_triage_accept_and_reject(app):
    _stage()
    assert qs.set_triage("/src/show-a", qs.TRIAGE_ACCEPTED).triage_status == "accepted"
    assert qs.set_triage("/src/show-a", qs.TRIAGE_REJECTED).triage_status == "rejected"


def test_triage_rejects_unknown_status(app):
    _stage()
    with pytest.raises(ValueError):
        qs.set_triage("/src/show-a", "maybe")


def test_triage_on_unknown_folder_returns_none(app):
    assert qs.set_triage("/src/never-analysed", qs.TRIAGE_ACCEPTED) is None


def test_triage_survives_reanalysis(app):
    """
    The user rejected the RECORDING, not the measurement.  Re-analysing must not
    silently reset that judgement back to pending.
    """
    _stage()
    qs.set_triage("/src/show-a", qs.TRIAGE_REJECTED)
    _stage()                                            # re-analyse same folder
    assert qs.get_staging("/src/show-a").triage_status == qs.TRIAGE_REJECTED


def test_list_staging_orders_best_first_errors_last(app):
    _stage("/src/good", crest_factor_db=18.0)
    _stage("/src/poor", crest_factor_db=5.0)
    qs.upsert_staging("/src/broken", source_dir="/src", error="no audio found")

    names = [r.name for r in qs.list_staging("/src")]
    assert names[0] == "good"
    assert names[-1] == "broken"          # errored rows sort last, never hidden


def test_failed_analysis_records_error(app):
    row = qs.upsert_staging("/src/broken", source_dir="/src", error="boom")
    assert row.error == "boom"
    assert row.listening_quality is None


# ═════════════════════════════════════════════════════════════════════════════
# Promotion — staging → permanent
# ═════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def recording(app):
    p = Artist(name="Test Act")
    _db.session.add(p)
    _db.session.commit()
    perf = Performance(artist_id=p.id)
    _db.session.add(perf)
    _db.session.commit()
    rec = Recording(performance_id=perf.id, folder_path="lib/test", quality="A-")
    _db.session.add(rec)
    _db.session.commit()
    return rec


def test_promote_copies_scores(app, recording):
    staged = _stage()
    perm = qs.promote_to_recording("/src/show-a", recording.id)
    assert perm.listening_quality == staged.listening_quality
    assert perm.score_tone == staged.score_tone
    assert perm.features_json == staged.features_json
    assert perm.recording_id == recording.id


def test_promote_backlinks_staging_row(app, recording):
    _stage()
    qs.promote_to_recording("/src/show-a", recording.id)
    assert qs.get_staging("/src/show-a").recording_id == recording.id


def test_promote_unanalysed_folder_is_a_noop(app, recording):
    """Ingest must never fail because a folder was not analysed."""
    assert qs.promote_to_recording("/src/never-analysed", recording.id) is None


def test_promote_is_idempotent(app, recording):
    _stage()
    qs.promote_to_recording("/src/show-a", recording.id)
    qs.promote_to_recording("/src/show-a", recording.id)
    rows = (_db.session.query(type(qs.get_for_recording(recording.id)))
            .filter_by(recording_id=recording.id).all())
    assert len(rows) == 1


def test_letter_grade_and_automated_score_coexist(app, recording):
    """
    The two measurements are separate by design: the letter grade covers
    performance quality too and stays a human judgement.
    """
    _stage()
    qs.promote_to_recording("/src/show-a", recording.id)
    assert recording.quality == "A-"                      # manual, untouched
    assert recording.quality_score.listening_quality is not None


def test_rescore_stored_updates_from_cached_features(app):
    """
    A score_version bump must be applyable WITHOUT re-decoding audio. That claim
    was documented in _is_current() long before anything implemented it, so a
    weight change silently left old rows showing the previous engine's numbers.
    """
    _stage()
    row = qs.get_staging("/src/show-a")
    row.listening_quality = 1.0          # pretend an older engine wrote this
    _db.session.commit()

    out = qs.rescore_stored()
    assert out["rescored"] >= 1
    assert qs.get_staging("/src/show-a").listening_quality != 1.0


def test_rescore_skips_rows_from_an_older_analyser(app):
    """
    Cached features from an older extractor lack whatever the new scoring reads.
    Those must be reported and skipped, never silently scored against missing
    inputs.
    """
    _stage()
    row = qs.get_staging("/src/show-a")
    row.analysis_version = "0"           # predates the current extractor
    before = row.listening_quality
    _db.session.commit()

    out = qs.rescore_stored()
    assert out["stale_features"] >= 1
    assert qs.get_staging("/src/show-a").listening_quality == before


def test_rescore_dry_run_writes_nothing(app):
    _stage()
    row = qs.get_staging("/src/show-a")
    row.listening_quality = 1.0
    _db.session.commit()

    qs.rescore_stored(dry_run=True)
    assert qs.get_staging("/src/show-a").listening_quality == 1.0


def test_favorite_defaults_off_and_is_independent(app, recording):
    """
    is_favorite is the THIRD quality signal and must not be entangled with the
    other two: analysis must never set it, and setting it must never disturb
    the letter grade or the automated score.
    """
    assert recording.is_favorite is False

    _stage()
    qs.promote_to_recording("/src/show-a", recording.id)
    assert recording.is_favorite is False        # analysis does not touch it

    recording.is_favorite = True
    _db.session.commit()
    assert recording.quality == "A-"
    assert recording.quality_score.listening_quality is not None


def test_favorite_can_contradict_both_other_signals(app, recording):
    """
    The whole point of the star: a rough tape of an unrepeatable night is a
    favorite AND a poor grade. Nothing may prevent that combination.
    """
    recording.quality = "C"
    recording.is_favorite = True
    _db.session.commit()
    assert recording.quality == "C" and recording.is_favorite is True


def test_deleting_recording_cascades_score_but_keeps_triage(app, recording):
    _stage()
    qs.promote_to_recording("/src/show-a", recording.id)
    rec_id = recording.id

    _db.session.delete(recording)
    _db.session.commit()

    assert qs.get_for_recording(rec_id) is None           # CASCADE
    assert qs.get_staging("/src/show-a") is not None      # SET NULL, decision kept


# ═════════════════════════════════════════════════════════════════════════════
# Serialisation
# ═════════════════════════════════════════════════════════════════════════════
def test_serialize_omits_features_unless_asked(app):
    _stage()
    row = qs.get_staging("/src/show-a")
    assert "features" not in qs.serialize(row)
    assert "features" in qs.serialize(row, include_features=True)


def test_serialize_staging_carries_triage_fields(app):
    _stage()
    out = qs.serialize(qs.get_staging("/src/show-a"))
    assert out["triage_status"] == qs.TRIAGE_PENDING
    assert out["folder_path"].endswith("show-a")
    assert out["sampled"]                                 # JSON round-tripped


def test_serialize_none_is_none():
    assert qs.serialize(None) is None


# ── Advanced Metrics grouping (2026-08-02) ────────────────────────────────────
# Metrics are now displayed UNDER the group they belong to, marked scored vs
# measured-only. That mapping lives in quality_interpret.METRIC_GROUP but the
# AUTHORITY is score_tone/score_noise/score_dynamics — if the two drift, the
# panel starts claiming a dead input moves the number, which is exactly the
# misreading that made presence balance look like a scoring bug on 2026-07-30.

def _features_consumed_by(func):
    """Feature keys a scoring function reads, straight out of its source."""
    import inspect, re
    return set(re.findall(r'f\.get\("([a-z0-9_]+)"\)', inspect.getsource(func)))


def test_every_displayed_metric_has_a_group():
    from app.utils.quality.quality_interpret import METRICS, METRIC_GROUP
    assert {k for k, _ in METRICS} == set(METRIC_GROUP), \
        "every metric in METRICS needs a METRIC_GROUP entry, and vice versa"
    assert all(g in ("tone", "noise", "dynamics", "other")
               for g, _ in METRIC_GROUP.values())


def test_scored_flag_matches_what_the_scoring_functions_actually_read():
    from app.utils.quality.quality_interpret import METRIC_GROUP
    from app.utils.quality.quality_scoring import (
        score_tone, score_noise, score_dynamics)

    consumed = {
        "tone":     _features_consumed_by(score_tone),
        "noise":    _features_consumed_by(score_noise),
        "dynamics": _features_consumed_by(score_dynamics),
    }
    for key, (group, scored) in METRIC_GROUP.items():
        if group == "other":
            assert not scored
            continue
        actually = key in consumed[group]
        assert actually == scored, (
            f"{key} is marked scored={scored} under {group}, but "
            f"score_{group}() {'does' if actually else 'does not'} read it")


def test_zero_weight_metrics_are_still_displayed():
    """Presence balance, midrange scoop and hum were demoted, not deleted.
    They are true measurements worth eyeballing; the panel just has to be
    honest that they carry no weight."""
    from app.utils.quality.quality_interpret import METRIC_GROUP
    for key in ("presence_balance_db", "midrange_scoop_db", "hum_ratio_db"):
        group, scored = METRIC_GROUP[key]
        assert group in ("tone", "noise") and scored is False


def test_every_scored_feature_is_actually_displayed():
    """
    The gap this catches, found 2026-08-02: hf_energy_ratio_db had been 50% of
    the Tone score since the 07-31 rework and appeared in no panel, so the Tone
    meter showed one of its two real inputs. A feature that moves the number
    must be visible, or the breakdown is not a breakdown.
    """
    from app.utils.quality.quality_interpret import METRIC_GROUP
    from app.utils.quality.quality_scoring import (
        score_tone, score_noise, score_dynamics)

    scored = set()
    for fn in (score_tone, score_noise, score_dynamics):
        scored |= _features_consumed_by(fn)
    assert scored <= set(METRIC_GROUP), (
        f"scored but undisplayed: {sorted(scored - set(METRIC_GROUP))}")


def test_hf_energy_ratio_bands_track_the_scoring_curve():
    """
    Display rungs were drawn from the 110-recording corpus AND aligned to the
    HF_RATIO curve knees, so the words and the score cannot tell different
    stories. Spot-check the two ends and the A/B boundary near -35 dB.
    """
    from app.utils.quality.quality_interpret import metric_rows
    from app.utils.quality.quality_scoring import curve, HF_RATIO

    def verdict(v):
        r = [x for x in metric_rows({"hf_energy_ratio_db": v})
             if x["key"] == "hf_energy_ratio_db"]
        assert r, "hf_energy_ratio_db must produce a display row"
        return r[0]["state"]

    assert verdict(-55.0) == "bad"     # curve ~20  — the "cigar box" end
    assert verdict(-45.0) == "poor"    # curve ~45
    assert verdict(-35.0) == "ok"      # curve ~72  — A/A- separation
    assert verdict(-20.0) == "good"    # curve  100
    # A "good" verdict must never sit below a "bad" one on the actual curve.
    assert curve(-20.0, HF_RATIO) > curve(-55.0, HF_RATIO)
