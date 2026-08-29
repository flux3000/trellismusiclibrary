"""
utils/track_signals.py — audio evidence that a track is not music (2026-08-28).

Live sets are full of tracks that are not songs: band introductions, tuning,
chatter, an announcement about the merch table. Trellis already detects those
from the TITLE (`utils/ingest.py::detect_track_flags`), which works whenever
the taper wrote "Band Intros" and not at all when they wrote "07".

This module supplies the second, independent opinion — read off the audio —
so the flag engine has something when the title says nothing.

  TWO MEASUREMENTS, BOTH RELATIVE TO THE RECORDING ITSELF

  * Relative duration. A song runs 3-6 minutes; banter runs 60-90 seconds.
    Measured against the show's own median, because a jazz set and a bluegrass
    set do not share an absolute song length.
  * Relative spectral flatness. Wiener entropy: how evenly energy is spread
    across the spectrum. Speech and applause are noise-like and flat; sustained
    instrumental tones are peaky and harmonic. Measured against the 25th
    percentile of the show's own tracks — see below for why that and not the
    median.

  WHAT WAS TRIED AND FAILED, MEASURED

  Absolute thresholds on anything. Validated against two COMPLETE recordings
  in the library — Alison Krauss 1992-07-17 (SBD, 24 tracks, 9 non-music) and
  Acoustic All-Stars 1995-10-13 (AUD, 23 tracks, 4 non-music). Speech-band
  energy ratio, zero-crossing rate, low-frequency content and onset periodicity
  all failed: the SBD/AUD gap in each is larger than the music/non-music gap,
  so a global threshold would mostly be detecting the microphone.

  FLATNESS ALONE ALSO FAILS, and this one cost a rewrite. On the soundboard it
  separates perfectly (music 0.0005-0.0021, non-music 0.0029-0.0206). On the
  audience tape the classes OVERLAP COMPLETELY (music 0.0048-0.0566, non-music
  0.0203-0.0346) — the non-music range sits entirely inside the music range,
  because crowd noise is present through every track of an AUD source and
  drowns exactly the distinction being measured.

  The first version of this module weighted flatness at 0.65 and reported 95%
  accuracy. That number was measured on a hand-picked SUBSET of each show —
  half non-music, which is nothing like a real recording — and on the complete
  shows it collapsed to 58% with ten false positives. Normalising against the
  MEDIAN was part of the same mistake: the median moves with how much non-music
  a recording contains, so the same track scores differently depending on its
  neighbours. The 25th percentile lands inside the music cluster whatever the
  mix, which is what a baseline has to do.

  Duration turned out to be the robust half and flatness the fragile one —
  the reverse of the original design. They are now weighted equally, which is
  what the evidence supports: duration alone is 96% on SBD and 100% on AUD;
  flatness adds the separation duration misses on clean sources.

  MEASURED PERFORMANCE (n=47, two complete recordings):
      music      0.00 – 0.48   (34 tracks)
      non-music  0.43 – 0.97   (13 tracks)
      98% accurate at threshold 0.50, ZERO false positives.

  The one miss is "Tuning, Intro By Bill Vernon" (0.43) — 2.7 minutes that is
  half real instrument tuning, so it has genuine harmonic content and runs
  long. Honestly ambiguous, and it errs toward "music", which is the safe
  direction: this signal must never tell someone their song is chatter.

  TWO COMPLETE RECORDINGS IS STILL A SMALL CORPUS, and both are acoustic
  bluegrass. An electric band with long feedback-heavy intros, or a set of
  short songs, has not been tested. This is deliberately a signal with a
  confidence attached, not a verdict, and nothing here writes a flag on its
  own — see suggest_non_music().
"""

# Relative flatness → evidence, 0..1. Ratio is against the 25th percentile of
# the recording's own flatness, which sits inside the music cluster whatever
# proportion of the show is talking. A song reads near 1x that baseline; speech
# on a clean source reads 3-25x.
_FLAT_REL = [(1.0, 0.00), (2.0, 0.30), (3.0, 0.60), (5.0, 0.90), (8.0, 1.00)]

# Relative duration → evidence, 0..1, against the recording's median track
# length. The robust half of the pair: alone it is 96% on the SBD corpus and
# 100% on the AUD one, where flatness is useless.
_DUR_REL = [(0.30, 1.00), (0.55, 0.85), (0.75, 0.55), (0.95, 0.20), (1.20, 0.00)]

# Equal. Swept jointly over both complete recordings; 0.50/0.50 was the only
# weighting that gave zero false positives on both.
_W_FLAT, _W_DUR = 0.50, 0.50

# The percentile used as the flatness baseline. NOT the median — see module
# docstring: the median moves with a recording's non-music share, so the same
# track scores differently depending on what sits beside it.
_FLAT_BASELINE_PCT = 25

# Above this, call it non-music. The highest-scoring SONG across both complete
# recordings is 0.48 ("Grant's Corner (played off mic)", which is genuinely
# unusual audio); the lowest non-music track we catch is 0.66. 0.50 sits in
# that gap, which buys the no-false-positives property that makes the signal
# safe to surface at all. Accuracy is flat from 0.49 to 0.55 — this is a plateau,
# not a knife edge.
NON_MUSIC_THRESHOLD = 0.50
# Comfortably clear of anything ambiguous — worth showing unprompted.
NON_MUSIC_STRONG = 0.70

# Mirrors TRACK_FLAGS[].nonMusic in app/static/js/app.js, which the Track model
# names as the single source of truth for the vocabulary.
NON_MUSIC_FLAGS = frozenset({
    "banter", "tuning", "audience", "announcement",
    "interview", "introduction", "band_intros",
})

# The recording's median is the whole basis of the comparison, so it needs a
# population. Under four tracks it is not a median, it is a coin toss.
MIN_TRACKS = 4


def _interp(x, anchors):
    """Piecewise-linear over (input, output) anchors, clamped at both ends."""
    if x is None:
        return None
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return anchors[-1][1]


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _percentile(values, pct):
    """Linear-interpolated percentile. Avoids a numpy import on this path."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * (pct / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def non_music_scores(tracks):
    """
    Score every track of ONE recording together.

    `tracks` is an iterable of (key, spectral_flatness, duration_s). Returns
    {key: {"score", "flat_rel", "dur_rel"}} — or {} when the recording is too
    small, or when the medians are unusable (an all-silent transfer would put
    the median at zero and every ratio at infinity).

    Deliberately takes the WHOLE recording rather than one track: a per-track
    function could not compute the median that makes this work at all, and
    offering one would invite exactly the absolute-threshold mistake the module
    docstring exists to warn about.
    """
    rows = [(k, f, d) for k, f, d in tracks if f is not None and d is not None]
    if len(rows) < MIN_TRACKS:
        return {}

    # Duration uses the median; flatness uses a LOW percentile. Not an
    # inconsistency — see the module docstring. Track length is symmetric
    # around the songs, so the median is the right centre; flatness is not,
    # because non-music sits on one side of it and drags the median with it.
    base_dur  = _median(d for _, _, d in rows)
    base_flat = _percentile([f for _, f, _ in rows], _FLAT_BASELINE_PCT)
    if not base_flat or not base_dur:
        return {}

    out = {}
    for key, flat, dur in rows:
        flat_rel = flat / base_flat
        dur_rel  = dur / base_dur
        score = (_W_FLAT * _interp(flat_rel, _FLAT_REL)
                 + _W_DUR * _interp(dur_rel, _DUR_REL))
        out[key] = {
            "score":    round(score, 3),
            "flat_rel": round(flat_rel, 3),
            "dur_rel":  round(dur_rel, 3),
        }
    return out


def suggest_non_music(score, title_flags):
    """
    Turn a score into a SUGGESTION, or nothing.

    Returns None, or {"confidence": "likely"|"strong", "score": float}.

    ENGINE-FACING ONLY. Nothing in the UI shows this (Ryan, 2026-08-28): the
    signal is here to inform the ingestion and metadata-assignment engines,
    not to sit a hedged second opinion next to a real flag on screen. It is
    currently unused pending that engine work; keep it, do not render it.

    Two rules, both deliberate:

      * It never names WHICH kind of non-music. The audio cannot tell banter
        from tuning from an announcement — only the title can, and that is
        detect_track_flags's job. Claiming otherwise would be inventing
        metadata.
      * It stays silent when the title already produced a non-music flag.
        Agreeing with a decision already made is noise, and the point of this
        signal is the case where the title said nothing.

    Nothing in this module writes a flag. It produces a suggestion for a person
    to accept, because the cost of a wrong auto-flag (a song filed as chatter,
    silently, in someone's library) is far higher than the cost of a click.
    """
    if score is None or score < NON_MUSIC_THRESHOLD:
        return None
    if title_flags and any(f in NON_MUSIC_FLAGS for f in title_flags):
        return None
    return {
        "confidence": "strong" if score >= NON_MUSIC_STRONG else "likely",
        "score": round(float(score), 3),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Measurement — deliberately librosa-free
# ═════════════════════════════════════════════════════════════════════════════
# This runs INSIDE the ingest, on the fast path, where importing librosa (and
# the numba/scipy stack behind it) would cost more than the measurement does.
# Verified against librosa.feature.spectral_flatness on the 12-track corpus
# above: agreement within 1.3% on every track, and 0.15 s/track against 0.27 s.
#
# Windowed rather than whole-track for the same reason the listening-quality
# extractor is: six 5-second samples answer this question as well as a full
# decode does — measured, same 11/12 on that show either way.

N_WINDOWS   = 6
WINDOW_SEC  = 5.0
_N_FFT      = 4096
_HOP        = 1024
_AMIN       = 1e-10


def measure_track(path):
    """
    (spectral_flatness, duration_s) for one audio file, or (None, None).

    Never raises: this is one input to a suggestion, and a file that will not
    decode should cost the ingest nothing at all.
    """
    try:
        import numpy as np
        import soundfile as sf
    except ImportError:
        return None, None
    try:
        with sf.SoundFile(path) as f:
            sr = f.samplerate
            duration = f.frames / float(sr)
            chunks = []
            start, end = 2.0, max(2.0, duration - WINDOW_SEC - 1.0)
            offsets = ([start] if end <= start else
                       [start + (end - start) * i / (N_WINDOWS - 1)
                        for i in range(N_WINDOWS)])
            for off in offsets:
                f.seek(int(off * sr))
                w = f.read(int(WINDOW_SEC * sr), dtype="float32", always_2d=True)
                if len(w) > sr:
                    chunks.append(w.mean(axis=1))
        if not chunks:
            return None, duration
        return _flatness(np.concatenate(chunks)), duration
    except Exception:  # noqa: BLE001
        return None, None


def _flatness(y):
    """
    Median per-frame spectral flatness: geometric mean over arithmetic mean of
    the power spectrum. Hann-windowed STFT via numpy's rfft — the same quantity
    librosa.feature.spectral_flatness(power=2.0) returns.
    """
    import numpy as np
    if y is None or len(y) < _N_FFT:
        return None
    win = np.hanning(_N_FFT + 1)[:-1]
    n_frames = 1 + (len(y) - _N_FFT) // _HOP
    if n_frames < 1:
        return None
    idx = np.arange(_N_FFT)[None, :] + _HOP * np.arange(n_frames)[:, None]
    power = np.maximum(np.abs(np.fft.rfft(y[idx] * win, axis=1)), _AMIN) ** 2
    geo = np.exp(np.mean(np.log(power), axis=1))
    ari = np.mean(power, axis=1)
    return float(np.median(geo / ari))
