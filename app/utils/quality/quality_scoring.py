"""
quality_scoring.py — Listening Quality score, v2.

A PURE FUNCTION over the raw features produced by quality_features.py. No audio
decode happens here, which is the point: retuning any curve or weight below is a
fast re-score pass over stored feature values.

Spec: Context Library/2026-07-27 - Recording Quality Score — v2 Build Brief (HANDOFF).md

──────────────────────────────────────────────────────────────────────────────
WHAT THIS IS FOR
──────────────────────────────────────────────────────────────────────────────
Ryan's design brief, which overrides any instinct toward technical
completeness:

    "The primary use case of this entire system is to LISTEN TO THE MUSIC that
     I have collected. I do not want to mess around with the lower quality
     things, life is too short. I want to be able to share with others who are
     definitely in that 99% who are biased against live recordings because they
     don't like the boominess, tinniness, and otherwise weird listening
     experience that many of them have."

Every complaint in that sentence is a TONE complaint. Hence Tone at half the
weight, and hence a couple of digital pops counting for nothing.

──────────────────────────────────────────────────────────────────────────────
STRUCTURE — two separate things
──────────────────────────────────────────────────────────────────────────────
  "Listening Quality"  0-100, from three groups:
        Tone      50%   presence balance, midrange scoop, spectral tilt
        Noise     30%   mid-band signal-to-noise, hum
        Dynamics  20%   crest factor

  "Technical Issues"   a pass/fail list that deducts ONLY when tripped.
        Clipping, dead channel, out of phase, dropouts.

All four Technical Issues scored clean across every recording tested so far.
They are insurance against the day something genuinely broken is ingested, not
discriminators — which is exactly why they are gates rather than weighted
metrics. As weighted metrics they were 25% of the score doing no work.

v1 had 16 measurements with hand-drawn curves and ~36 tuned numbers, fitted
against 13 human grades. That was far too many knobs for the evidence. This is
6 curves and 4 gates.

──────────────────────────────────────────────────────────────────────────────
SCALE — criterion-referenced, NOT norm-referenced
──────────────────────────────────────────────────────────────────────────────
Curves are anchored to perception, not to the rest of the library.

  100 means "at or beyond the point where further improvement is inaudible."
  It does NOT mean "best recording I have."

So ties at the top are correct; a score never changes because something else
was ingested; and scores stay comparable across artists and meaningful to a
peer node with a different library.

PRECISION: reported to one decimal because that is useful for ranking, but the
honest resolution is roughly +/-2 points.
"""

import re

import numpy as np

QUALITY_SCORE_VERSION = "3"


def curve(x, points):
    """Piecewise-linear interpolation over (input, score) anchors. Clamped."""
    if x is None:
        return None
    return float(np.clip(np.interp(x, [p[0] for p in points],
                                   [p[1] for p in points]), 0.0, 100.0))


def _geo(parts):
    """
    Weighted GEOMETRIC mean over (score, weight) pairs, skipping unavailable
    metrics.

    Geometric rather than arithmetic because listening is gated by the WORST
    problem, not the average of all of them. A recording with pristine dynamics
    and no treble is not "average", it is unpleasant. The two means agree within
    a point when components are balanced and diverge sharply only when one
    collapses — which is the whole behaviour we want, from one line rather than
    a table of special-case caps.
    """
    live = [(s, w) for s, w in parts if s is not None]
    if not live:
        return None
    tw = sum(w for _, w in live)
    return float(np.exp(sum(w * np.log(max(s, 1.0)) for s, w in live) / tw))


# ═════════════════════════════════════════════════════════════════════════════
# TONE  (50%)
# ═════════════════════════════════════════════════════════════════════════════

# Presence balance: 2-6 kHz minus 250-800 Hz, in dB. The tinniness detector.
#
# Strongly ASYMMETRIC on purpose — nearly flat on the dark side, steep on the
# bright side. Justified twice over: perceptually harshness drives a listener
# away far faster than dullness does, and empirically this feature correlates
# NEGATIVELY with Ryan's grades (r = -0.42), i.e. darker reads as better.
#
# The decisive evidence was a controlled listen. Hammersmith (-19.9 dB) against
# Denver (-13.7 dB) on headphones: Ryan confirmed Hammersmith is audibly
# "muddier" — so the measurement is TRUE — and graded it an A anyway. The
# feature was right and the curve was wrong.
# v2 re-anchored on AUDIBILITY rather than corpus position. The previous curve
# mapped +2.7 dB to 10/100 — implying "about as bad as a recording can sound" —
# and that was drawn back when bandwidth metrics were still in this group
# propping the 1970 Georgia Tech tape up. With those removed it overshot wildly:
# Georgia Tech scored 36.6 against Ryan's C (63).
#
# +2.7 dB is "noticeably bright and thin", not unlistenable. The bottom of the
# scale is now reserved for balances that would genuinely be brutal (+10 dB and
# beyond). Georgia Tech now lands at 56 against a target of 63.
PRESENCE = [(-30, 82), (-24, 94), (-20, 100), (-8, 100), (-5, 92), (-3, 80),
            (0, 62), (3, 42), (6, 25), (10, 10), (15, 0)]

# Midrange scoop: low mids vs the mean of their neighbours. Negative = scooped,
# the hollow "smiley-face EQ" character that travels with tinniness.
# The single best raw predictor in the entire feature set (r = +0.60).
SCOOP = [(-10, 3), (-8, 5), (-6, 20), (-4, 40), (-2, 70), (0, 100), (6, 100)]

# Spectral tilt, dB/octave over 200 Hz - 8 kHz. Music is naturally pink-ish;
# both "too dark" and "no bottom end" are penalised.
TILT = [(-20, 0), (-18, 5), (-15, 22), (-12, 45), (-9, 70), (-7, 90),
        (-5, 100), (-3, 95), (-2, 80), (-1, 65), (0, 50)]

# Energy above 8 kHz relative to total — is there any top end at all.
#
# RESTORED to the Tone group 2026-07-28. The v2 simplification dropped every
# bandwidth measure because they correlated poorly (r = +0.106) with the
# original 20 grades. That left Tone unable to see "this recording has no
# treble", and it showed: the 1992 Danny Gatton tape measures 6.4 kHz cutoff
# and -48.5 dB above 8 kHz — Ryan calls it "made inside a cigar box" — yet
# scored 77.2, ABOVE the 1988 recording he grades an A.
#
# The original 20 simply had nothing that dark except Watkins Glen, so the
# correlation test could not see the metric's value. Restoring it fixed the
# ordering AND raised correlation across the 20 from 0.823 to 0.860.
HF_RATIO = [(-70, 0), (-60, 12), (-52, 28), (-45, 45), (-40, 58), (-35, 72),
            (-30, 83), (-25, 92), (-20, 100), (-10, 100)]

# ═════════════════════════════════════════════════════════════════════════════
# NOISE  (30%)
# ═════════════════════════════════════════════════════════════════════════════

# Signal-to-noise across 1-8 kHz.
#
# Measured in a band every recording actually occupies, whatever its bandwidth.
# An earlier version measured 10-16 kHz and was badly wrong: a recording
# band-limited to 8 kHz has neither music NOR much noise up there, so the ratio
# collapsed and it scored as hissy. We were measuring silence and calling it
# noise. The 1981 Bushnell tape read 13.0 dB that way; its real 1-8 kHz SNR is
# 27.0, the cleanest in its corpus, and Ryan graded it an A.
MID_SNR = [(6, 3), (10, 15), (13, 28), (16, 42), (19, 57), (22, 70), (25, 82),
           (28, 92), (32, 100), (40, 100)]

# Mains hum: dB above the local spectral median at 50/60 Hz and harmonics.
# DISPLAY ONLY as of 2026-07-31 (r = -0.038 against 113 grades). Curve retained
# so the Advanced Metrics row can still be colour-coded.
HUM = [(0, 100), (6, 100), (12, 80), (20, 50), (30, 20), (40, 0)]

# Crowd SNR: programme against the noise floor in 250-2500 Hz, the band where
# an audience actually lives (chatter, shouting, applause). Added 2026-07-31 to
# close the audience-tape blind spot — within AUD recordings this is the best
# predictor we have (r = +0.318) while bandwidth measures are flat or even
# non-monotonic across AUD grades.
#
# Anchors are set to the observed corpus range (14.5-31.4 dB) read as audibility
# rather than as percentile position: below ~15 dB the room is competing with
# the band, above ~28 dB the audience has stopped being a factor at all.
CROWD_SNR = [(10, 0), (14, 15), (17, 35), (20, 55), (23, 72), (26, 86),
             (28, 95), (31, 100), (40, 100)]

# ═════════════════════════════════════════════════════════════════════════════
# DYNAMICS  (20%)
# ═════════════════════════════════════════════════════════════════════════════

# Crest factor = peak minus RMS, in dB. How far transients stand above the
# average level — "punch", or inversely, "how squashed".
#
# Genuinely diagnostic here because the #1 cassette-audience defect is deck
# automatic gain control riding the level and crushing the room's dynamics.
# Very high values are suspicious rather than good: usually a very quiet
# transfer, or a single click inflating the peak.
CREST = [(4, 0), (6, 10), (9, 30), (13, 75), (16, 95), (19, 100), (22, 100),
         (26, 85), (30, 60), (36, 40)]


# ═════════════════════════════════════════════════════════════════════════════
# Group scoring
# ═════════════════════════════════════════════════════════════════════════════
# Reweighted 2026-07-28 from 50/30/20 after Ryan suggested dynamics was being
# undervalued. He was right, and by a wide margin — 40/20/40 gave the lowest
# average error of any combination tested against the n=20 fit (4.14 vs 5.35),
# tied the best correlation (0.860), and was the only weighting that put his
# stated best and worst Danny Gatton recordings first and last respectively.
#
# Noise dropping to 20% fit what the data had been saying independently: since
# the hum fix, hum barely varies across recordings, and mid-band SNR has been
# the metric most often at odds with what Ryan actually hears.
#
# Reweighted AGAIN 2026-07-30 to Dynamics 50 / Tone 35 / Noise 15 — Ryan's
# explicit call as the new default, made while discussing the Gatton labels
# (1/25/79 = B, 12/3/88 = A) rather than a re-run of the n=20/n=24 fit. Treat
# as the current default until re-verified against the fit — flag to Ryan
# rather than reverting unilaterally if a re-run says otherwise.
GROUP_WEIGHTS = {"tone": 0.35, "noise": 0.15, "dynamics": 0.50}


def _arith(parts):
    """
    Weighted ARITHMETIC mean, used WITHIN a group.

    Groups combine geometrically (worst problem gates the score) but their
    sub-metrics combine arithmetically, and the distinction is deliberate.
    Within a group the sub-metrics are three views of ONE phenomenon — spectral
    balance, or noise — so averaging them is the right way to read that
    phenomenon. Between groups they are independent failure modes, so gating on
    the worst is right.

    v1 used geometric everywhere, added specifically because Georgia Tech's
    excellent HF extension was averaging away its harsh balance. That reason is
    gone: bandwidth metrics are no longer in the Tone group, so all three
    members now measure the same thing and cannot mask each other. Keeping
    geometric here made Georgia Tech 26 points harsher than Ryan's grade.
    """
    live = [(s, w) for s, w in parts if s is not None]
    if not live:
        return None
    return sum(s * w for s, w in live) / sum(w for _, w in live)


def score_tone(f):
    """
    Presence balance and midrange scoop REMOVED from scoring 2026-07-31.

    They held 60% of this group (0.35 + 0.25) on the strength of correlations
    measured at n=13 and n=20. Against the full 113 graded recordings in the
    library they carry no signal at all:

        presence balance   r = +0.057
        midrange scoop     r = -0.016

    and cross-validated together as a two-feature model they score r = -0.223,
    i.e. worse than predicting the mean. The earlier r = +0.60 for scoop and
    r = -0.42 for presence were small-sample artefacts.

    This is also the whole explanation of the Danny Gatton inversion that
    tabled the score on 2026-07-30. The PRESENCE curve slopes negative — darker
    reads as better — but for soundboards the true relationship is POSITIVE
    (r = +0.41): brighter grades better. Gatton's recordings are SBDs, so the
    curve was rewarding exactly the wrong direction, scoring his worst tape 100
    and his A-grade shows 43 and 21. No interaction term is needed and none
    should be attempted: there is no signal in these two inputs to interact.

    Both remain in quality_features.py and are still shown under Advanced
    Metrics. They are measurements; they are simply not evidence of quality.

    What is left is the two validated tonal measures, weighted by their
    correlation with grade (tilt +0.283, HF ratio +0.327 — near enough equal).
    """
    return _arith([
        (curve(f.get("spectral_tilt_db_oct"), TILT), 0.50),
        (curve(f.get("hf_energy_ratio_db"), HF_RATIO), 0.50),
    ])


def score_noise(f):
    """
    Hum dropped from 0.35 to display-only 2026-07-31; crowd SNR takes its place.

    Hum correlates r = -0.038 with grade across 113 recordings — nothing. It
    barely varies between recordings anyway once the 2026-07-28 fix stopped it
    counting sustained bass notes, so it was 35% of this group doing no work.

    Crowd SNR (250-2500 Hz, the voice band) correlates +0.286 overall and
    +0.318 within audience tapes, where it is the single best predictor
    available. Weighted below mid-band SNR because it is new and unproven
    across a wide corpus.
    """
    return _arith([
        (curve(f.get("mid_snr_db"), MID_SNR), 0.60),
        (curve(f.get("crowd_snr_db"), CROWD_SNR), 0.40),
    ])


def score_dynamics(f):
    return curve(f.get("crest_factor_db"), CREST)


# ═════════════════════════════════════════════════════════════════════════════
# Technical Issues — pass/fail, deduct only when tripped
# ═════════════════════════════════════════════════════════════════════════════
def technical_issues(f):
    """
    Returns (total_deduction, [issue dicts]).

    Normally returns (0.0, []) — every recording tested so far is clean on all
    four. These exist for genuinely broken material.

    Deduction sizes are a starting proposal and have never been exercised
    against a real failing recording. Revisit when one turns up.
    """
    issues = []

    clip = f.get("clipping_pct")
    if clip is not None and clip > 0.01:
        # Run-qualified clipping only: isolated full-scale samples are
        # normalisation, not damage. Scale severity, cap at -25.
        d = min(25.0, 5.0 + 20.0 * min(1.0, (clip - 0.01) / 0.5))
        issues.append({"issue": "Clipping", "detail": f"{clip:.3f}% of samples",
                       "deduction": round(d, 1)})

    ch_min = f.get("channel_rms_min_db")
    if ch_min is not None and ch_min < -60:
        issues.append({"issue": "Dead channel", "detail": f"{ch_min:.0f} dBFS",
                       "deduction": 30.0})

    ph = f.get("phase_correlation")
    if ph is not None and ph < -0.3:
        issues.append({"issue": "Out of phase", "detail": f"correlation {ph:+.2f}",
                       "deduction": 20.0})

    drop = f.get("dropout_count")
    if drop is not None and drop >= 3:
        issues.append({"issue": "Dropouts", "detail": f"{int(drop)} gaps",
                       "deduction": 10.0})

    return sum(i["deduction"] for i in issues), issues


# ═════════════════════════════════════════════════════════════════════════════
# Informational flags — never affect the score ("nerd zone")
# ═════════════════════════════════════════════════════════════════════════════
def informational_flags(f):
    """
    Provenance curiosities, never scored.

    DC offset and mains frequency used to live here as pills. They are now
    ordinary rows in the Advanced Metrics table instead — a value with a
    verdict reads better than a bare pill, and DC offset has a real range worth
    colour-coding. What remains here is genuinely rare and genuinely binary, so
    this list is normally empty.
    """
    out = []
    if f.get("likely_lossy_source"):
        out.append(f"Likely lossy source ({f.get('lossy_tier_est') or 'unknown tier'})")
    if f.get("bit_depth_padded"):
        out.append(f"Bit depth padded (effective {f.get('effective_bit_depth')})")
    if f.get("upsampled"):
        out.append("Upsampled sample rate")
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Composite
# ═════════════════════════════════════════════════════════════════════════════
# ═════════════════════════════════════════════════════════════════════════════
# Source adjustment + band verdict  (2026-07-31)
# ═════════════════════════════════════════════════════════════════════════════
# Recording source is the single strongest predictor of Ryan's grade that we
# have. On its own it cross-validates at r = +0.314 — better than any individual
# acoustic feature — and adding it to the feature set lifts CV correlation from
# +0.431 to +0.512, the largest single improvement measured on 2026-07-31.
#
# Mean grade points by source across 113 recordings:
#     MTX 96.0 · FM 94.2 · SBD 91.7 · AUD 83.2
#
# IMPORTANT — this is one shared model with source as an input, NOT a separate
# scale per source. Separate per-source models were tested and are worse:
# SBD +0.484 but AUD -0.015 and FM +0.043, because splitting an already thin
# corpus starves each bucket. Ryan raised the separate-scale idea directly; the
# data says adjust, do not fork.
#
# Offsets are expressed against the SBD baseline and are deliberately SMALL
# relative to the raw grade differences above — most of the AUD/SBD gap is real
# acoustic difference the features already see, and double-counting it would
# push every audience tape into RED.
SOURCE_OFFSET = {"SBD": 0.0, "FM": +1.5, "MTX": +2.0, "AUD": -3.0}


def _source_offset(source):
    """Normalise a free-text source string ('FM Peterw', 'Radio') to an offset."""
    if not source:
        return 0.0
    s = str(source).strip().upper()
    for key in ("MTX", "SBD", "AUD", "FM"):
        if s.startswith(key):
            return SOURCE_OFFSET[key]
    if s.startswith("RADIO"):
        return SOURCE_OFFSET["FM"]
    return 0.0


# Collector folder naming puts the source in parentheses: "… (SBD)", "(AUD)",
# "(FM)", "(MTX)", often decorated — "(SBD A+)", "(MTX - Baker)", "(FM A-)".
#
# This lives in the ENGINE rather than in either caller because both need it and
# they must not disagree: quality analysis runs at TRIAGE time, before any
# Recording row exists, so the folder name is the only place source can come
# from. Once ingested, `recording.source` is authoritative and should be passed
# explicitly instead.
#
# Anchored to a word boundary so a venue like "Audimax" is not read as an
# audience tape, and alternation is longest-first so "MTX" cannot lose to a
# stray substring.
_SOURCE_IN_NAME = re.compile(r"\((?:[^)]*\b)?(MTX|SBD|AUD|FM)\b[^)]*\)", re.I)


def guess_source_from_name(name):
    """Best-effort source from a folder name. None when it cannot be read."""
    m = _SOURCE_IN_NAME.search(name or "")
    return m.group(1).upper() if m else None


# ── Calibration onto the grade scale ─────────────────────────────────────────
# The composite is criterion-referenced: its curves are anchored to audibility,
# not to library position, which is what makes a score stable and portable to a
# peer node. That is worth keeping, but it also means the composite does not
# live on the same scale as a letter grade — removing presence/scoop in v3 moved
# the whole distribution down, and fixed band thresholds became far too harsh.
#
# So the two concerns are kept separate:
#     listening_quality  raw composite, criterion-referenced, meaning unchanged
#     predicted_grade    the composite mapped onto grade points, for banding
#
# The map is a plain 2-parameter least-squares fit over 113 graded recordings
# (2026-07-31): grade = 0.5956 * composite + 40.592, giving MAE 6.95 against
# 8.21 for the v2 engine. Two parameters over 113 points is not something that
# can meaningfully overfit — unlike the per-metric curve shapes, which is where
# the earlier overfitting actually happened.
#
# REFIT THIS when the labelled corpus grows materially (Ryan is adding grades).
# tools/quality/labelled_corpus_2026-07-31.json + the standalone app are the
# place to do it; rescoring needs no audio decode.
CALIBRATION_SLOPE = 0.5956
CALIBRATION_INTERCEPT = 40.592


def predicted_grade(lq):
    """Composite -> predicted grade points (A+ = 100, A- = 85, B = 70, C = 55)."""
    if lq is None:
        return None
    return float(np.clip(CALIBRATION_SLOPE * lq + CALIBRATION_INTERCEPT, 0.0, 100.0))


# Three-band triage verdict, thresholded on PREDICTED GRADE POINTS so the bands
# mean exactly what they say:
#     GREEN  -> would grade A- or better   (worth ingesting on sight)
#     YELLOW -> would grade B+/B           (give it a listen first)
#     RED    -> would grade below B        (probably not worth the time)
#
# WHY BANDS AND NOT A DECIMAL, in the app: against 113 graded recordings the
# engine reaches r = 0.55 with a mean absolute error near 7 grade points. One
# decimal place on a number routinely 7 points out is false precision — 75.7 vs
# 75.0 is noise, not a B against a C. Three bands are what the evidence
# supports, and triage (worth my time or not) was always the real decision at
# ingest.
#
# The engine still RETURNS the number. The standalone tool at tools/quality/
# remains the development surface where the quantitative score keeps being
# worked on, per Ryan 2026-07-31 — this restriction is a presentation choice in
# the app, not a capability removed from the engine.
BAND_GREEN, BAND_YELLOW = 85.0, 70.0


def verdict_band(lq):
    """GREEN / YELLOW / RED. Takes the RAW composite; bands on predicted grade."""
    pg = predicted_grade(lq)
    if pg is None:
        return None
    if pg >= BAND_GREEN:
        return "green"
    return "yellow" if pg >= BAND_YELLOW else "red"


# Deliberately neutral, non-prescriptive labels (Ryan, 2026-08-02). The earlier
# set ("Worth ingesting" / "Probably not worth the time") made a judgement the
# engine is not entitled to make: every recording here is worth ingesting to
# somebody, and the band is a DETECTION of measured audio character, not a
# recommendation. High/Medium/Low describes what was measured and leaves the
# decision where it belongs — with the archivist.
BAND_LABEL = {"green": "High", "yellow": "Medium", "red": "Low"}


def score_recording(f, preset="listener", source=None):
    """
    Score a raw-feature dict.

    `preset` retained for API compatibility.
    `source` is the recording's source string (SBD / AUD / FM / MTX). Optional:
    omitted it simply contributes no adjustment, so existing callers keep
    working unchanged.
    """
    if "error" in f:
        return {"error": f["error"]}

    groups = {"tone": score_tone(f), "noise": score_noise(f),
              "dynamics": score_dynamics(f)}
    base = _geo([(groups[k], GROUP_WEIGHTS[k]) for k in groups])

    deduction, issues = technical_issues(f)
    offset = _source_offset(source if source is not None else f.get("source"))
    lq = (None if base is None
          else float(np.clip(base + offset - deduction, 0.0, 100.0)))
    band = verdict_band(lq)

    pg = predicted_grade(lq)

    return {
        "listening_quality": round(lq, 1) if lq is not None else None,
        "predicted_grade": round(pg, 1) if pg is not None else None,
        "verdict_band": band,
        "verdict_label": BAND_LABEL.get(band),
        "source_offset": offset,
        "score_tone": round(groups["tone"], 1) if groups["tone"] is not None else None,
        "score_noise": round(groups["noise"], 1) if groups["noise"] is not None else None,
        "score_dynamics": round(groups["dynamics"], 1) if groups["dynamics"] is not None else None,
        "technical_issues": issues,
        "technical_deduction": round(deduction, 1),
        "flags": informational_flags(f),
        "score_version": QUALITY_SCORE_VERSION,
    }
