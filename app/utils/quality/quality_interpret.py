"""
quality_interpret.py — turns Listening Quality numbers into plain English.

Rule-driven only. There is no per-recording prose anywhere: a score maps to a
band, and the band supplies the sentence. That keeps every explanation
consistent, reviewable in one place, and impossible to drift out of sync with
the number it is describing.

Bands are the same everywhere: 90+, 80-90, 70-80, 60-70, 50-60, under 50.
"""

BANDS = [(90, "excellent"), (80, "good"), (70, "fair"),
         (60, "poor"), (50, "bad"), (0, "severe")]


def band(score):
    """Return the band key for a 0-100 score."""
    if score is None:
        return None
    for floor, key in BANDS:
        if score >= floor:
            return key
    return "severe"


# ── Overall ──────────────────────────────────────────────────────────────────
OVERALL = {
    "excellent": "An excellent-sounding recording. Nothing about the sound "
                 "should get between a listener and the performance.",
    "good":      "A good-sounding recording. Minor character of its own, but "
                 "comfortable to sit through from start to finish.",
    "fair":      "A listenable recording with obvious character. Fine on its "
                 "own terms, but not one to hand to a skeptic first.",
    "poor":      "Rough. Worth hearing for the performance, but the sound "
                 "itself will be a distraction for most listeners.",
    "bad":       "A difficult listen. Only worth it if you specifically want "
                 "this show.",
    "severe":    "Very hard to listen to. Archival interest only.",
}

# ── Tone (50% of the score) ──────────────────────────────────────────────────
TONE = {
    "excellent": "Natural, well-balanced tone. The frequency balance never "
                 "calls attention to itself.",
    "good":      "Slightly colored but comfortable — a mild tilt you stop "
                 "noticing within a minute.",
    "fair":      "Noticeably colored. A little bright and thin, or a little "
                 "thick, without being fatiguing.",
    "poor":      "Clearly unbalanced. Expect boominess or an edgy top end "
                 "that grows tiring over a full show.",
    "bad":       "Poor balance — hollow, honky or harsh enough to be the "
                 "first thing you notice.",
    "severe":    "Severely unbalanced. Tinny, muddy or boxy to the point of "
                 "being hard to sit through.",
}

# ── Noise (30%) ──────────────────────────────────────────────────────────────
NOISE = {
    "excellent": "Essentially silent behind the music.",
    "good":      "Very clean. Any hiss sits well below the performance.",
    "fair":      "Mild background hiss — audible in quiet passages, easy to "
                 "ignore once the music starts.",
    "poor":      "Noticeable hiss or hum. Present through quiet moments and "
                 "obvious between songs.",
    "bad":       "Intrusive noise. Hiss or mains hum competes with quiet "
                 "passages.",
    "severe":    "Heavy noise — a constant layer of hiss or hum across the "
                 "whole performance.",
}

# ── Dynamics (20%) ───────────────────────────────────────────────────────────
DYNAMICS = {
    "excellent": "Fully open dynamics. Transients land with real impact and "
                 "quiet passages stay quiet.",
    "good":      "Good dynamic range with only light compression.",
    "fair":      "Somewhat compressed. Loud and quiet passages sit closer "
                 "together than they should.",
    "poor":      "Noticeably squashed — typically a tape deck's automatic "
                 "gain control riding the level.",
    "bad":       "Heavily compressed. Flat, with little sense of light and "
                 "shade.",
    "severe":    "Crushed. The performance has been flattened to a near-"
                 "constant level.",
}

GROUPS = {"tone": TONE, "noise": NOISE, "dynamics": DYNAMICS}

GROUP_LABEL = {"tone": "Tone", "noise": "Noise", "dynamics": "Dynamics"}
GROUP_BLURB = {
    "tone":     "Frequency balance — the boominess, tinniness and hollowness "
                "that make many live recordings unpleasant. Half the score.",
    "noise":    "Hiss, mains hum and background noise sitting under the music.",
    "dynamics": "How much life is left in the loud-to-quiet range, or how "
                "squashed the recording is.",
}

# What a Technical Issue means, in one line each
ISSUE_TEXT = {
    "Clipping":     "The signal was pushed past maximum and the waveform tops "
                    "are flattened, which sounds like a hard edge on peaks.",
    "Dead channel": "One stereo channel is effectively silent.",
    "Out of phase": "The two channels are wired against each other. On "
                    "speakers this hollows out the center of the image.",
    "Dropouts":     "Short gaps of digital silence interrupt the audio.",
}


# ═════════════════════════════════════════════════════════════════════════════
# Quick-glance: frequency cutoff and what it means
# ═════════════════════════════════════════════════════════════════════════════
def cutoff_verdict(f):
    """
    The one thing most people actually want to know: was this MP3-sourced?

    IMPORTANT: the cutoff frequency alone does not answer that. A 1970s
    audience cassette genuinely has nothing above 13 kHz — that is the medium,
    not a transcode. What identifies a lossy encoder is a CLIFF: a drop of
    35 dB or more inside 500 Hz, sitting in the 15.5-20.5 kHz band, with dead
    flat nothing above it. Analogue rolloff is a gentle slope over several kHz.

    So we report the cutoff number, and separately report the verdict, which
    comes from the wall test in quality_features._lossy().
    """
    edge = f.get("hf_edge_hz")
    lossy = f.get("likely_lossy_source")
    tier = f.get("lossy_tier_est")

    if edge is None:
        return {"khz": None, "verdict": "Unknown", "detail": "", "state": "ok"}

    khz = round(edge / 1000.0, 1)

    if lossy:
        # "short" is what fits in the quick-glance bar; "verdict" is the fuller
        # phrasing used in the tooltip heading.
        short = (tier or "MP3").split(" / ")[0]
        return {"khz": khz, "verdict": f"Lossy source — {tier or 'MP3'}",
                "short": short, "state": "bad",
                "detail": f"A sharp encoder wall sits at {khz} kHz with nothing "
                          "above it. This was encoded to a lossy format at some "
                          "point, even if it is now in a lossless container."}

    if edge >= 19000:
        d = "Content runs to the top of the audible range with no encoder wall."
        state = "good"
    elif edge >= 15000:
        d = ("Full-bandwidth for practical purposes, and the rolloff is gradual "
             "rather than a sharp encoder wall.")
        state = "good"
    elif edge >= 11000:
        d = ("Some high end missing, but it rolls off gradually — the signature "
             "of tape or a limited capture chain, not lossy encoding.")
        state = "ok"
    elif edge >= 7000:
        d = ("Noticeably limited high end, gradually rolled off. Typical of "
             "older analogue sources. Not an encoder wall.")
        state = "ok"
    else:
        d = ("Very restricted high end. The recording will sound dull, but the "
             "rolloff is gradual so this is the source, not lossy encoding.")
        state = "poor"
    return {"khz": khz, "verdict": "No lossy signature", "short": "No MP3 wall",
            "state": state, "detail": d}


# ═════════════════════════════════════════════════════════════════════════════
# Advanced metrics — what each one is, and where a value sits in its range
# ═════════════════════════════════════════════════════════════════════════════
# Each entry: label, unit, what it measures in one sentence, and a ladder of
# (upper_bound, state, description). The first rung whose bound the value is
# below wins. State drives the colour; description is shown on hover.
METRICS = [
    ("crowd_snr_db", {
        "label": "Crowd / room noise", "unit": " dB",
        "about": "How far the music sits above the noise floor in 250–2500 Hz, "
                 "the band an audience occupies — chatter, shouting, applause. "
                 "Added 2026-07-31 because the engine was blind within audience "
                 "tapes; this is the best AUD predictor found (r = +0.32).",
        "scale": [(15, "bad",  "Audience competing with the band"),
                  (18, "poor", "Audience clearly present"),
                  (22, "ok",   "Some room noise"),
                  (27, "good", "Quiet room"),
                  (99, "good", "Audience essentially absent")],
    }),
    ("noise_nonstationarity_db", {
        "label": "Noise steadiness", "unit": " dB",
        "about": "How much the noise floor fluctuates. Tape hiss and mains hum "
                 "are steady and easy to ignore; a room full of people talking "
                 "is not. Two recordings can share a noise floor where one is "
                 "benign and the other ruins the show. Measured and shown, "
                 "but not currently a scored input — crowd/room noise (above) "
                 "is the scored audience-tape signal.",
        "scale": [(4,  "good", "Steady — hiss-like, easy to ignore"),
                  (6,  "ok",   "Mildly variable"),
                  (8,  "poor", "Fluctuating — live room audible"),
                  (99, "bad",  "Very unsteady — intrusive audience")],
    }),
    ("modulation_index", {
        "label": "Clarity (articulation)", "unit": "",
        "about": "How deep the envelope valleys are between notes, measured at "
                 "2–20 Hz. Reverberation and microphone distance fill those "
                 "gaps in, so a low value means a distant or washy capture. A "
                 "stand-in for direct-to-reverberant ratio, which needs an "
                 "impulse response we will never have. Measured and shown, "
                 "but not currently a scored input.",
        "dp": 2,
        "scale": [(0.35, "bad",  "Very washy / distant"),
                  (0.50, "poor", "Reverberant"),
                  (0.70, "ok",   "Moderately articulate"),
                  (1.00, "good", "Clear, well-articulated"),
                  (99,   "good", "Very dry and close")],
    }),
    ("presence_balance_db", {
        # DEMOTED 2026-07-31 — display only, zero weight. r = +0.057 against
        # 113 grades. Retained because it is a true measurement and useful to
        # eyeball; it is simply not evidence of quality.
        "label": "Presence balance", "unit": " dB",
        "about": "How loud the 2–6 kHz presence region is versus the low mids. "
                 "Human hearing peaks around 2–5 kHz, so an excess here reads "
                 "as tinny and fatiguing. NOTE: measured but NOT scored — "
                 "against 113 graded recordings this correlates +0.06 with "
                 "listening quality, and its old curve caused the 2026-07-30 "
                 "Danny Gatton inversion.",
        "scale": [(-26, "ok",   "Very dark"),
                  (-20, "good", "Dark"),
                  (-8,  "good", "Natural"),
                  (-3,  "ok",   "Slightly forward"),
                  (0,   "poor", "Bright"),
                  (99,  "bad",  "Harsh, tinny")],
    }),
    ("midrange_scoop_db", {
        # DEMOTED 2026-07-31 alongside presence balance — display only, zero
        # weight. r = -0.016 against 113 graded recordings, essentially no
        # signal. The "about" text below used to call this "the single
        # strongest predictor of a poor grade in testing", which was true of
        # the v1 fit it was written for and became actively false the day
        # this was pulled from scoring — caught 2026-08-09 (Ryan: contradicted
        # its own zero-weight marker, and the panel's footnote promises the
        # tooltip explains why every unscored row is unscored).
        "label": "Midrange scoop", "unit": " dB",
        "about": "Whether the 250–800 Hz body sits below both its neighbours. "
                 "A scoop can make a recording sound hollow and boxy. NOTE: "
                 "measured but NOT scored — against 113 graded recordings "
                 "this correlates -0.02 with listening quality, essentially "
                 "no signal, so it was removed from Tone in the 2026-07-31 "
                 "rework alongside presence balance.",
        "scale": [(-6, "bad",  "Severely hollow"),
                  (-4, "poor", "Hollow"),
                  (-2, "poor", "Scooped"),
                  (0,  "ok",   "Slightly scooped"),
                  (99, "good", "Full midrange")],
    }),
    ("spectral_tilt_db_oct", {
        "label": "Spectral tilt", "unit": " dB/oct",
        "about": "The overall slope from bass to treble. Music is naturally "
                 "pink-ish, around −3 to −7 dB per octave. Steeper is dull; "
                 "flatter is thin.",
        "scale": [(-14, "bad",  "Very muffled"),
                  (-10, "poor", "Dull"),
                  (-7,  "ok",   "Dark"),
                  (-3,  "good", "Natural"),
                  (-2,  "ok",   "Slightly thin"),
                  (99,  "poor", "Thin, no bass")],
    }),
    ("hf_energy_ratio_db", {
        # Added to the display 2026-08-02. It has been 50% of the Tone score
        # since the 07-31 rework but appeared in no panel, so Tone showed one
        # of its two real inputs — invisible until metrics were nested under
        # their group and the hole became obvious.
        #
        # Rungs are drawn from the 110-recording labelled corpus
        # (tools/quality/labelled_corpus.json) and aligned to the knees of the
        # HF_RATIO curve in quality_scoring.py, so the words and the score
        # cannot disagree. Corpus: min -59.7, p10 -44.4, p25 -39.2, median
        # -31.3, p75 -25.9, p90 -22.3, max -11.2; r = +0.31 against grade,
        # the strongest single tonal predictor measured.
        #
        # Grade means bear the rungs out: A+ -29.7, A -29.9, A- -36.1,
        # B+ -34.3, B -39.3. The A/B separation sits right around -35.
        "label": "HF energy ratio", "unit": " dB",
        "about": "Energy above 8 kHz relative to the whole signal — whether "
                 "there is any top end at all, as opposed to where it stops "
                 "(that is Frequency cutoff). Half the Tone score. Restored to "
                 "scoring 2026-07-28 after the engine proved unable to see "
                 "'this recording has no treble': the 1992 Danny Gatton tape "
                 "Ryan calls 'made inside a cigar box' reads -48.5 dB here.",
        "scale": [(-50, "bad",  "Almost no top end"),
                  (-40, "poor", "Little treble"),
                  (-32, "ok",   "Modest treble"),
                  (-24, "good", "Healthy top end"),
                  (99,  "good", "Full, open treble")],
    }),
    ("mid_snr_db", {
        "label": "Signal-to-noise", "unit": " dB",
        "about": "How far the music sits above the noise floor across 1–8 kHz "
                 "— the band carrying most musical information and where hiss "
                 "is most audible.",
        "scale": [(12, "bad",  "Heavy hiss"),
                  (16, "poor", "Noticeable hiss"),
                  (22, "ok",   "Some hiss"),
                  (28, "good", "Clean"),
                  (99, "good", "Very clean")],
    }),
    ("hum_ratio_db", {
        "label": "Mains hum", "unit": " dB",
        "about": "How far the 50/60 Hz hum line and its harmonics rise above "
                 "the surrounding spectrum. Caused by ground loops in the "
                 "recording chain. Measured and shown, but not currently a "
                 "scored input.",
        "scale": [(6,  "good", "None"),
                  (12, "ok",   "Trace"),
                  (20, "poor", "Audible"),
                  (99, "bad",  "Strong")],
    }),
    ("crest_factor_db", {
        "label": "Crest factor", "unit": " dB",
        "about": "Peak level minus average level — how much dynamic range "
                 "survives. Low values mean compression, often a tape deck's "
                 "automatic gain control crushing the room.",
        "scale": [(9,  "bad",  "Heavily compressed"),
                  (13, "poor", "Compressed"),
                  (16, "ok",   "Moderate"),
                  (22, "good", "Open"),
                  (26, "good", "Very open"),
                  (99, "ok",   "Suspiciously high")],
    }),
    ("hf_edge_hz", {
        "label": "Frequency cutoff", "unit": " Hz",
        "about": "The highest frequency still carrying real musical content, "
                 "after subtracting the noise floor. Measured and shown — it "
                 "also drives the quick-glance cutoff badge at the top of the "
                 "card — but not currently a scored input here; WHERE the "
                 "signal stops is a different question from HF energy ratio "
                 "(above), which is scored and asks whether there's any top "
                 "end at all.",
        "scale": [(5000,  "bad",  "Very restricted"),
                  (8000,  "poor", "Restricted"),
                  (11000, "ok",   "Limited"),
                  (15000, "good", "Good extension"),
                  (99999, "good", "Full bandwidth")],
    }),
    ("lufs_integrated", {
        "label": "Loudness", "unit": " LUFS",
        "about": "Perceived loudness on the broadcast standard scale. Only "
                 "extremes matter — very quiet wastes headroom, very loud "
                 "means someone squashed it. Measured and shown, but not "
                 "currently a scored input — crest factor (above) is the "
                 "scored compression signal.",
        "scale": [(-26, "poor", "Very quiet"),
                  (-20, "ok",   "Quiet"),
                  (-10, "good", "Well-leveled"),
                  (-8,  "ok",   "Hot"),
                  (99,  "poor", "Slammed")],
    }),
    ("dc_offset", {
        "label": "DC offset", "unit": "", "dp": 5, "abs": True,
        "about": "Whether the waveform sits off-centre instead of swinging "
                 "symmetrically around zero. Caused by a faulty converter or "
                 "preamp. It wastes headroom and clicks at edit points — but "
                 "it is fixable in seconds with a high-pass filter, so it "
                 "never affects the score.",
        "scale": [(0.001, "good", "Centered"),
                  (0.01,  "ok",   "Slightly off-center"),
                  (9,     "poor", "Off-center")],
    }),
]

# Mains frequency (50 vs 60 Hz) was displayed here briefly and removed
# 2026-07-28. It told you which electrical grid the hum sat on, which is a
# geographic curiosity and not a listening-quality fact — nothing about it
# helps decide whether to play a recording. The value is still computed and
# stored (it falls out of the hum measurement for free) but nothing reads it.


# Which group each metric belongs under, and whether it actually MOVES that
# group's score (2026-08-02, Ryan: "are these groupable into the three
# categories?"). Mostly yes — with two honest exceptions kept in "other",
# because filing them under a group would imply they contribute to it.
#
#   scored=True   this feature is an input to score_<group>() — see
#                 quality_scoring.py, which is the authority here
#   scored=False  measured and displayed, weight 0
#
# The zero-weight ones are not filler. Presence balance and midrange scoop were
# 60% of Tone until 2026-07-31, when 113 graded recordings showed they carry no
# signal (r = +0.057 / −0.016) and that presence was the cause of the Gatton
# inversion. Hum was 35% of Noise on the same evidence (r = −0.038). They stay
# visible because they are true measurements — they are simply not evidence of
# quality, and the panel now says which is which rather than showing eleven
# numbers of apparently equal standing.
METRIC_GROUP = {
    "spectral_tilt_db_oct":     ("tone", True),
    "hf_energy_ratio_db":       ("tone", True),
    "presence_balance_db":      ("tone", False),
    "midrange_scoop_db":        ("tone", False),
    "hf_edge_hz":               ("tone", False),
    "mid_snr_db":               ("noise", True),
    "crowd_snr_db":             ("noise", True),
    "noise_nonstationarity_db": ("noise", False),
    "modulation_index":         ("noise", False),
    "hum_ratio_db":             ("noise", False),
    "crest_factor_db":          ("dynamics", True),
    "lufs_integrated":          ("dynamics", False),
    # Filed under Dynamics rather than left ungrouped (Ryan, 2026-08-02: group
    # them all). It is the honest closest fit — DC offset is a level-domain
    # fault that eats headroom, which is Dynamics' territory — but it is a
    # converter/preamp defect rather than a musical reading, and it never
    # affects the score. The "not scored" marker on the row carries that.
    "dc_offset":                ("dynamics", False),
}


# Which scoring curve each SCORED metric is read through. The curve is the
# authority on what a reading is worth; the "scale" ladders above are only the
# authority on what to CALL it.
#
# Until 2026-08-28 the two were independent and disagreed. A recording measuring
# 24.7 dB mid-band SNR was labelled "Clean" in green — the ladder's second-from-
# top rung, and its top two rungs are BOTH "good" — while MID_SNR put the same
# reading at 80.8/100, and 23.3 dB crowd SNR read "Quiet room" in green while
# CROWD_SNR put it at 73.4, which is amber on the app's own bands. Their Noise
# meter came out at 77.8 (amber) under two green readings, with nothing on
# screen to explain it (Ryan, 2026-08-28). Colour now comes from the curve, so
# a reading can never be a different colour from the meter it feeds.
def _metric_curves():
    """
    (curve_fn, {metric_key: anchors}) — imported lazily because quality_scoring
    pulls in numpy and this module otherwise does not, which is what keeps app
    boot off the analysis dependencies.

    Only the five SCORED metrics appear. A key missing here means the metric has
    no curve, which is the same thing as carrying no weight.
    """
    from app.utils.quality.quality_scoring import (
        curve, TILT, HF_RATIO, MID_SNR, CROWD_SNR, CREST)
    return curve, {
        "spectral_tilt_db_oct": TILT,
        "hf_energy_ratio_db":   HF_RATIO,
        "mid_snr_db":           MID_SNR,
        "crowd_snr_db":         CROWD_SNR,
        "crest_factor_db":      CREST,
    }


# Four bands over a metric's own 0-100 curve position.
#
# NOT the 90/80/60 the UI uses for group meters and the overall score, which is
# what this function tried first and got wrong. Those are bands on a SCORE; a
# single metric's curve position is a different quantity, and reading it on the
# score ruler pushed a third to a half of every metric's range into a different
# colour than the corpus-derived display ladder gives it — 39% of the range for
# HF energy ratio alone.
#
# These boundaries come from the ladders themselves. The rungs in METRICS were
# drawn from the 110-recording corpus and, for hf_energy_ratio_db, deliberately
# aligned to the HF_RATIO curve's knees — a decision recorded in
# tests/test_quality.py::test_hf_energy_ratio_bands_track_the_scoring_curve,
# which pins -55/-45/-35/-20 dB to bad/poor/ok/good. 85/60/35 reproduces all
# four, and disagrees with the existing ladders on 8-29% of their range rather
# than 34-45%.
#
# What this does and does not buy: a reading can no longer be GREEN under an
# amber meter, which was the reported defect — 24.7 dB mid-band SNR was labelled
# "Clean" in green at a true 80.8, and 23.3 dB crowd SNR "Quiet room" in green at
# 73.4, under a 77.8 Noise meter. Both now read one band down. It does NOT
# guarantee a reading always shares its meter's exact colour: a weighted mean of
# two numbers can land in a lower band than either input, and no threshold
# choice fixes arithmetic.
def _state_from_subscore(sub):
    if sub is None:
        return None
    if sub >= 85:
        return "good"
    if sub >= 60:
        return "ok"
    if sub >= 35:
        return "poor"
    return "bad"


def metric_rows(f, scored_only=False):
    """
    Build display rows for the Advanced Metrics panel.

    `scored_only=True` drops every zero-weight reading. Triage passes it: a
    number on the triage page is there to justify a meter, and five of the
    eleven justify nothing (Ryan, 2026-08-28 — "if something does not
    contribute to the score, remove that metric from view here"). View
    Recording still shows the full set, where the question is "what IS this
    recording" rather than "why this score".
    """
    curve_fn, curves = None, None
    rows = []
    for key, m in METRICS:
        v = f.get(key)
        if v is None:
            continue
        group, scored = METRIC_GROUP.get(key, ("other", False))
        if scored_only and not scored:
            continue
        cmpv = abs(v) if m.get("abs") else v
        state, desc = m["scale"][-1][1], m["scale"][-1][2]
        for bound, st, d in m["scale"]:
            if cmpv < bound:
                state, desc = st, d
                break
        # The WORD still comes from the ladder — "Clean" is the right thing to
        # call 24.7 dB. Only the colour moves to the curve.
        sub = None
        if scored:
            if curves is None:
                curve_fn, curves = _metric_curves()
            pts = curves.get(key)
            if pts is not None:
                sub = curve_fn(v, pts)
                state = _state_from_subscore(sub) or state
        rows.append({
            "key": key, "label": m["label"], "value": v, "unit": m["unit"],
            "dp": m.get("dp"), "abs": bool(m.get("abs")),
            "state": state, "verdict": desc, "about": m["about"],
            "group": group, "scored": scored,
            # 0-100 on this metric's own scoring curve. Present only for scored
            # metrics; it is what `state` is now derived from.
            "sub_score": round(sub, 1) if sub is not None else None,
            "scale": [{"upto": b, "state": s2, "text": d} for b, s2, d in m["scale"]],
        })
    return rows


def interpret(result):
    """
    Take a score_recording() result and return display-ready text.

    Returns {"overall": {...}, "groups": [{...}], "issues": [{...}]}
    """
    lq = result.get("listening_quality")
    out = {
        "overall": {
            "score": lq,
            "band": band(lq),
            "text": OVERALL.get(band(lq), ""),
        },
        "groups": [],
        "issues": [],
    }
    for key in ("tone", "noise", "dynamics"):
        s = result.get(f"score_{key}")
        b = band(s)
        out["groups"].append({
            "key": key,
            "label": GROUP_LABEL[key],
            "score": s,
            "band": b,
            "text": GROUPS[key].get(b, ""),
            "blurb": GROUP_BLURB[key],
        })
    for i in result.get("technical_issues", []):
        out["issues"].append({**i, "text": ISSUE_TEXT.get(i["issue"], "")})
    return out


def interpret_full(scored, feats, scored_only=False):
    """interpret() plus the quick-glance strip and Advanced Metrics rows."""
    out = interpret(scored)
    out["cutoff"] = cutoff_verdict(feats)
    out["metrics"] = metric_rows(feats, scored_only=scored_only)
    out["quick"] = {
        "format": feats.get("format"),
        "bitrate_kbps": feats.get("bitrate_kbps"),
        "sample_rate_hz": feats.get("sample_rate_hz"),
        "bit_depth": feats.get("effective_bit_depth"),
    }
    return out
