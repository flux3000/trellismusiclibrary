"""
quality_features.py — Raw feature extraction for the Recording Quality Score.

Deliberately depends only on numpy / scipy / soundfile / pyloudnorm.
Librosa is NOT used: everything here is STFT, filtering and statistics, and
soundfile's native seek lets us decode only the windows we care about.

Spec: Context Library/Recording Quality Score — Design Spec v1.md

Feature extraction is kept strictly separate from scoring. This module answers
"what is physically true about this audio"; quality_scoring.py answers "how
good is that". Keeping them apart is what lets weights be retuned later with
no audio decode.
"""

import os
import glob
import json
import numpy as np
from scipy import signal
import soundfile as sf

# Only for naming what WAS found when nothing FLAC-decodable was (the .shn/
# .ape/etc formats select_tracks below doesn't read). Deliberately not a
# structural dependency the other direction — ingest.py has no reason to
# import anything from the quality engine.
from app.utils.ingest import RESOLVE_AUDIO_EXTS

# Bumped to "2" on 2026-07-31: crowd_snr_db, noise_nonstationarity_db and
# modulation_index were added to analyse_window(). Rows extracted at version 1
# do not contain them, so they must be re-decoded rather than merely re-scored —
# this is the one kind of change that a score-only rescore cannot fix.
QUALITY_ANALYSIS_VERSION = "2"

# ── Sampling parameters (spec §Sampling strategy) ────────────────────────────
# Sampling spreads across TRACKS, not just within one.
#
# The v1 design sampled 3 windows of a single "representative" track, on the
# premise that quality is a capture-chain property and therefore constant across
# a show. That holds for noise and tonal balance — but NOT for HF extension.
# Measured across the 1983 Hammersmith soundboard, hf_edge ran 7079 Hz on one
# track and 15703 Hz on another: same tape, same night, same transfer. Bandwidth
# tracks what the musicians were playing, not how it was captured.
#
# 3 tracks x 2 windows x 20 s covers both axes for ~120 s of decode.
#
# Tested at 2 tracks (2026-07-28) against all 20 graded recordings: average
# error rose 4.94 -> 6.19, worst case 7.9 -> 13.3, correlation 0.857 -> 0.740,
# and within-corpus ranking collapsed to chance on two of three artists.
# Scores moved 2.7 points on average, above the +/-2 measurement noise floor.
# Not worth the ~5 s saved. Decode is not the bottleneck anyway — see below.
N_TRACKS       = 3      # distinct tracks to sample
N_WINDOWS      = 2      # windows per track
WINDOW_SEC     = 20.0   # seconds per window
EDGE_SKIP_SEC  = 15.0   # ignore this much at track head/tail
MIN_TRACK_SEC  = 240.0  # 4 minutes — see select_tracks()
SILENT_RMS_DB  = -55.0  # windows quieter than this are skipped as non-programme

NOISE_WIN_SEC  = 0.400  # noise-floor analysis window (spec: 400 ms)
NOISE_PCTILE   = 10     # quietest decile

# Click detection is DISABLED in the score (weight 0 — it failed twice, once
# reporting 1646 clicks/min and later 11223 clicks/min on clean recordings, and
# it carries a systematic bias against plucked strings).
#
# It was nevertheless still being COMPUTED, and it is by far the most expensive
# thing here: profiled at 2.72 s per 20-second window against 0.16 s to decode
# that window, i.e. 16.3 s of an 18.1 s analysis — 90% of the runtime spent on
# a number nothing reads. LPC at order 32, refit every second, is simply slow.
#
# Set this True to bring it back when the detector is rebuilt to require clicks
# to be spectrally broadband as well as narrow.
COMPUTE_CLICKS = False


# ═════════════════════════════════════════════════════════════════════════════
# ITU-R 468 noise weighting
# ═════════════════════════════════════════════════════════════════════════════
# Standard 468 response table, dB relative to 1 kHz. Applied as a
# frequency-domain weighting over the power spectrum rather than as a filter —
# equivalent for our purposes and far simpler.
#
# NOTE (documented simplification, see spec): we use the 468 *weighting curve*
# with RMS detection, not the full quasi-peak ballistic. Valid for relative
# ranking; not a spec-compliant 468 measurement.
_ITU468_HZ = np.array([
    31.5, 63, 100, 200, 400, 800, 1000, 2000, 3150, 4000, 5000, 6300,
    7100, 8000, 9000, 10000, 12500, 14000, 16000, 20000, 31500])
_ITU468_DB = np.array([
    -29.9, -23.9, -19.8, -13.8, -7.8, -1.9, 0.0, 5.6, 9.0, 10.5, 11.7, 12.2,
    12.0, 11.4, 10.1, 8.1, 0.0, -5.3, -11.7, -22.2, -42.7])


def itu468_weights(freqs):
    """Linear power-gain weights for `freqs` (Hz) under ITU-R 468."""
    f = np.clip(freqs, _ITU468_HZ[0], _ITU468_HZ[-1])
    db = np.interp(np.log10(f), np.log10(_ITU468_HZ), _ITU468_DB)
    return 10.0 ** (db / 10.0)          # power domain


def _db(x, floor=-140.0):
    """Amplitude/power-safe dB with a low clamp (spec: floor lowered to −120+)."""
    x = np.asarray(x, dtype=float)
    return float(np.maximum(10.0 * np.log10(np.maximum(x, 1e-20)), floor))


# ═════════════════════════════════════════════════════════════════════════════
# Track + window selection
# ═════════════════════════════════════════════════════════════════════════════
def _other_audio_extensions(folder):
    """
    What audio-*like* files actually sit in `folder` when select_tracks()
    found no .flac. Mirrors select_tracks' own root-then-recurse preference,
    for the same reason: an un-flattened recording has an empty root, and a
    folder with its own (unsupported) audio shouldn't get bothered by
    whatever sits in a nested duplicate below it.

    Returns a sorted list of extensions (e.g. [".shn"]), or [] when the
    folder really does hold nothing audio-shaped — the plain "no flac files"
    case, same as always.
    """
    safe = glob.escape(folder)
    other_exts = RESOLVE_AUDIO_EXTS - {".flac"}

    def _scan(pattern):
        found = set()
        for p in glob.glob(pattern, recursive=True):
            ext = os.path.splitext(p)[1].lower()
            if ext in other_exts:
                found.add(ext)
        return found

    found = _scan(os.path.join(safe, "*"))
    if not found:
        found = _scan(os.path.join(safe, "**", "*"))
    return sorted(found)


def select_tracks(folder):
    """
    Choose which tracks to sample. Three rules, deliberately simple.

      1. Drop the first and last track
      2. Keep tracks >= 4 minutes
      3. From what remains, take 3 spread across the running order

    Rule 2 is doing the work that DB track flags used to do. Banter, tuning,
    stage announcements and applause tracks are almost always short, so
    duration alone separates music from non-music reliably — and it needs no
    audio decode and no metadata, which matters because flags will NOT be
    populated at the time this runs.

    Deliberately NOT used as a selector: HF edge. It swings 2x between tracks
    of the same show (measured: 7079 Hz to 15703 Hz across one soundboard), so
    it is far too noisy to gate on.

    Each rule is dropped in turn if it would empty the set, so a 3-track
    recording still gets scored.

    Returns ([(path, duration_sec), ...], note) or (None, reason).
    """
    # Root files first; only recurse if the root has none.
    #
    # Recursing unconditionally was wrong. It was added because un-flattened
    # recordings keep their audio in CD1/ CD2/ subdirs (the 1979 Balboa Jazz
    # Club one), and a root-only glob skipped them entirely. But some folders
    # contain a NESTED DUPLICATE — the 1976 Paris recording has an "(FM A)"
    # folder inside it holding another copy of the same 11 tracks — and
    # recursing there made select_tracks choose from 22 files spanning two
    # different transfers, shifting that recording's score by 2.4 points.
    #
    # Preferring the root resolves both: an un-flattened recording has an empty
    # root and falls through to the recursive search; a folder with its own
    # audio ignores whatever is nested below it.
    # glob.escape() on the FOLDER only (2026-07-31). Collector folder names
    # routinely carry [FLAC], [SBD], [EAC-FLAC] — and glob reads [...] in the
    # directory portion as a character class, so
    # "Grant Green ... (1972) [FLAC]" was searched as ".../(1972) F/", ".../(1972) L/"
    # etc. None exist, so the recording reported "no flac files" and silently
    # scored nothing. The *pattern* half must stay unescaped to keep globbing.
    safe = glob.escape(folder)
    paths = sorted(glob.glob(os.path.join(safe, "*.flac")))
    if not paths:
        # Path-string sort keeps discs in order: CD1/* then CD2/*.
        paths = sorted(glob.glob(os.path.join(safe, "**", "*.flac"), recursive=True))
    if not paths:
        other = _other_audio_extensions(folder)
        if other:
            return None, "no flac files (found %s instead — not supported yet)" % ", ".join(other)
        return None, "no flac files"

    durations = {}
    for p in paths:
        try:
            durations[p] = sf.info(p).duration
        except Exception:
            continue
    paths = [p for p in paths if p in durations]
    if not paths:
        return None, "no readable flac files"

    trimmed = paths[1:-1] if len(paths) > 2 else []
    long_only = [p for p in trimmed if durations[p] >= MIN_TRACK_SEC]

    # Progressive relaxation, strictest first
    for cand, note in ((long_only, ""),
                       (trimmed, "relaxed: kept tracks under 4 min"),
                       (paths, "relaxed: kept first/last track")):
        if cand:
            eligible = cand
            break
    else:
        return None, "no eligible tracks"

    # Spread across the running order rather than taking one "representative"
    # track — bandwidth in particular varies enormously song to song.
    n = min(N_TRACKS, len(eligible))
    idx = (np.unique(np.linspace(0, len(eligible) - 1, n + 2)[1:-1].round().astype(int))
           if len(eligible) > n else np.arange(len(eligible)))
    return [(eligible[i], durations[eligible[i]]) for i in idx][:n], note


# Backwards-compatible alias — older callers used the singular name
select_track = select_tracks


def window_offsets(duration, n=N_WINDOWS):
    """Evenly spaced window start offsets within the edge-trimmed span."""
    usable_start = EDGE_SKIP_SEC
    usable_end   = max(EDGE_SKIP_SEC, duration - EDGE_SKIP_SEC - WINDOW_SEC)
    if usable_end <= usable_start:
        return [max(0.0, (duration - WINDOW_SEC) / 2.0)]
    span = usable_end - usable_start
    fracs = [(i + 1) / (n + 1) for i in range(n)]
    return [usable_start + span * f for f in fracs]


def read_window(path, offset, dur=WINDOW_SEC):
    """Decode one window as (samples, sr) float64, shape (n, channels)."""
    with sf.SoundFile(path) as f:
        sr = f.samplerate
        f.seek(int(offset * sr))
        x = f.read(int(dur * sr), dtype="float64", always_2d=True)
    return x, sr


# ═════════════════════════════════════════════════════════════════════════════
# Per-window feature extraction
# ═════════════════════════════════════════════════════════════════════════════
def analyse_window(x, sr):
    """
    Extract every raw feature from a single window.
    `x` is (n_samples, n_channels) float in [-1, 1].
    """
    out = {}
    mono = x.mean(axis=1)
    n = len(mono)

    # ── Spectrum (shared by clarity, noise, lossy detection) ─────────────────
    nperseg = 4096
    freqs, _, Z = signal.stft(mono, fs=sr, nperseg=nperseg,
                              noverlap=nperseg // 2, window="hann")
    P = np.abs(Z) ** 2                     # power spectrogram (bins, frames)
    avg_p = P.mean(axis=1)

    # ── NOISE: per-bin minimum statistics ────────────────────────────────────
    # Taking the quietest *frames* fails on continuous loud music — there are no
    # quiet frames, so the 5th percentile just measures quieter music. Instead
    # take the low percentile of each frequency bin independently across time
    # (Martin's minimum-statistics idea). Music in any given bin is transient;
    # hiss and hum are continuous, so the per-bin floor tracks the noise even
    # under sustained programme material.
    w468 = itu468_weights(freqs)
    noise_spec = np.percentile(P, NOISE_PCTILE, axis=1)     # per-bin floor
    out["noise_floor_468_db"] = _db((noise_spec * w468).sum() / w468.sum())
    out["program_468_db"]     = _db((avg_p * w468).sum() / w468.sum())
    out["snr_468_db"]         = out["program_468_db"] - out["noise_floor_468_db"]

    # Hiss must be measured against the PROGRAMME IN THE SAME BAND, not against
    # total energy. Measured against the total, a recording with no treble at all
    # scores as the cleanest in the library — Watkins Glen read −98 dB "hiss"
    # purely because it has nothing above 5 kHz to be hissy in.
    #
    # hf_snr_db asks the right question: in the 10–16 kHz band, how far does the
    # music sit above the noise? A flat hiss shelf with no music on top gives ~0.
    hi = (freqs >= 10000) & (freqs <= 16000)
    if hi.any():
        prog_hi = max(avg_p[hi].mean(), 1e-20)
        noise_hi = max(noise_spec[hi].mean(), 1e-20)
        out["hf_snr_db"] = float(10 * np.log10(prog_hi / noise_hi))
        out["hiss_db"] = _db(noise_hi / max(avg_p.mean(), 1e-20))
    else:
        out["hf_snr_db"], out["hiss_db"] = 0.0, -140.0

    # PRIMARY noise metric: signal-to-noise across 1-8 kHz.
    #
    # hf_snr_db (10-16 kHz) was the primary measure through rev 3 and it was
    # wrong. A recording band-limited to 8 kHz has neither music NOR much noise
    # up at 10-16 kHz, so the ratio collapses and the recording scores as hissy
    # when in truth there is simply nothing there. The 1981 Bushnell tape read
    # 13.0 dB "HF SNR" while its real 1-8 kHz SNR is 27.0 dB — the best in its
    # corpus, and Ryan graded it an A.
    #
    # 1-8 kHz carries the bulk of musical information, is where hiss is most
    # audible, and — critically — every recording has content there regardless
    # of bandwidth. Measuring in a band the recording actually occupies is the
    # difference between measuring noise and measuring silence.
    mid = (freqs >= 1000) & (freqs <= 8000)
    if mid.any():
        out["mid_snr_db"] = float(10 * np.log10(
            max(avg_p[mid].mean(), 1e-20) / max(noise_spec[mid].mean(), 1e-20)))
    else:
        out["mid_snr_db"] = 0.0

    # Programme spectrum = average minus the estimated noise floor. Used for the
    # clarity measures so a hiss shelf is never mistaken for treble content.
    prog_p = np.maximum(avg_p - noise_spec, 1e-20)

    # Rumble: sub-25 Hz energy relative to total. Cut at 25 rather than 30 Hz
    # to stay clear of a 5-string bass low B (~31 Hz) and kick fundamentals —
    # below 25 Hz there is essentially no musical content, only HVAC, stage
    # thump, mic handling and tape-transport noise.
    lo = freqs < 25
    out["rumble_db"] = _db(avg_p[lo].sum() / max(avg_p.sum(), 1e-20))

    # ── CLARITY ──────────────────────────────────────────────────────────────
    # All clarity measures run on the NOISE-SUBTRACTED programme spectrum.
    cumulative = np.cumsum(prog_p)
    total = cumulative[-1]
    # 95%, not 85%. Musical energy is overwhelmingly bass-dominated, so an 85%
    # rolloff sits near 1 kHz for almost everything and discriminates nothing.
    out["rolloff_95_hz"] = float(freqs[np.searchsorted(cumulative, 0.95 * total)])
    hf = freqs >= 8000
    out["hf_energy_ratio_db"] = _db(prog_p[hf].sum() / max(total, 1e-20))

    # HF edge: highest frequency where real programme content still survives,
    # within 50 dB of the spectral peak. This is what separates a muffled reel
    # from an open FM broadcast.
    sdb = 10 * np.log10(prog_p)
    sdb_s = signal.savgol_filter(sdb, 31, 3) if len(sdb) > 31 else sdb
    active = np.where(sdb_s > sdb_s.max() - 50.0)[0]
    out["hf_edge_hz"] = float(freqs[active[-1]]) if len(active) else 0.0

    # Spectral tilt: dB per octave, least-squares fit over 200 Hz – 8 kHz
    band = (freqs >= 200) & (freqs <= 8000)
    if band.sum() > 10:
        lf = np.log2(freqs[band])
        ldb = 10.0 * np.log10(prog_p[band])
        out["spectral_tilt_db_oct"] = float(np.polyfit(lf, ldb, 1)[0])
    else:
        out["spectral_tilt_db_oct"] = 0.0

    # ── AUDIENCE / ROOM (added 2026-07-31) ───────────────────────────────────
    # Why these exist: validation against 113 graded recordings showed the
    # engine is BLIND within audience tapes (grade correlation -0.015 across
    # n=42 AUD). Bandwidth, which discriminates soundboards strongly (HF edge
    # 15186 Hz at A+ down to 7866 Hz at B+ and below), is flat and even
    # non-monotonic across AUD grades: 13010 / 12248 / 13353. A bad audience
    # tape has roughly the same bandwidth as a good one.
    #
    # What actually separates them is crowd noise and room reverberation, and
    # nothing here measured either. These three features close that gap. They
    # are extraction-only until validated against the corpus — the lesson of
    # 2026-07-31 is that a metric earns its weight with evidence, not intent.

    # Crowd noise lives in the voice band. Deliberately 250-2500 Hz rather than
    # mid_snr's 1-8 kHz: chatter, shouting and applause concentrate below
    # 2.5 kHz, while 1-8 kHz is chosen to span musical content and so dilutes
    # exactly the band of interest.
    cb = (freqs >= 250) & (freqs <= 2500)
    if cb.any():
        out["crowd_snr_db"] = float(10 * np.log10(
            max(avg_p[cb].mean(), 1e-20) / max(noise_spec[cb].mean(), 1e-20)))
    else:
        out["crowd_snr_db"] = 0.0

    # Crowd noise FLUCTUATES; tape hiss and mains hum do not. Two recordings can
    # share a noise floor where one is hiss (benign, easy to ignore) and the
    # other is an audience talking through the show (ruinous). Spread of the
    # quietest quartile of frames separates them: stationary noise gives a
    # narrow spread, a live room a wide one.
    if cb.any():
        fe = P[cb].sum(axis=0)
        quiet = fe[fe <= np.percentile(fe, 25)]
        if len(quiet) > 4:
            out["noise_nonstationarity_db"] = float(10 * np.log10(
                max(np.percentile(quiet, 90), 1e-20) /
                max(np.percentile(quiet, 10), 1e-20)))
        else:
            out["noise_nonstationarity_db"] = 0.0

    # Clarity / direct-to-reverberant, estimated without an impulse response.
    #
    # A true C50/C80 needs an IR we will never have. The standard substitute —
    # the modulation transfer idea behind STI — works on ordinary programme
    # audio: reverberation and microphone distance FILL THE GAPS between notes,
    # so the amplitude envelope's fluctuation shrinks. A close, dry capture
    # keeps deep valleys between transients; a distant one in a live room does
    # not. Band-limited to 2-20 Hz because that is the rate at which notes and
    # syllables arrive; below 2 Hz is musical phrasing (a slow ballad is not a
    # reverberant recording) and above 20 Hz is pitch, not envelope.
    hop = max(1, int(sr * 0.005))            # 5 ms  -> 200 Hz envelope rate
    win = max(2, int(sr * 0.010))            # 10 ms analysis window
    if n > win + hop * 128:
        env = np.convolve(mono ** 2, np.ones(win) / win, mode="valid")[::hop]
        env_mean = float(env.mean())
        if env_mean > 1e-20 and len(env) > 64:
            fs_env = sr / hop
            lo_m, hi_m = 2.0, min(20.0, fs_env / 2 * 0.9)
            if hi_m > lo_m:
                b, a = signal.butter(2, [lo_m / (fs_env / 2), hi_m / (fs_env / 2)],
                                     btype="band")
                ac = signal.filtfilt(b, a, env - env_mean)
                # Modulation index: RMS of the band-limited fluctuation over the
                # mean intensity. ~1.0 = strongly articulated, near 0 = smeared.
                out["modulation_index"] = float(
                    np.sqrt(np.mean(ac ** 2)) / env_mean)

    # ── TONAL BALANCE ────────────────────────────────────────────────────────
    # Extension alone is monotonic — more treble always scores higher — and that
    # is wrong. Too much high end relative to the body of the sound is "tinny",
    # and it is a far bigger listenability problem than a slightly dull tape.
    # These two measures are UNIMODAL: deviation in either direction costs.
    def _band_db(lo, hi):
        m = (freqs >= lo) & (freqs < hi)
        return 10 * np.log10(max(prog_p[m].sum(), 1e-20) / max(total, 1e-20)) if m.any() else -140.0

    e_bass     = _band_db(60, 250)
    e_lowmid   = _band_db(250, 800)
    e_mid      = _band_db(800, 2000)
    e_presence = _band_db(2000, 6000)

    # Presence balance: 2–6 kHz against the 250–800 Hz body. The ear is most
    # sensitive at 2–5 kHz, so more energy there than in the low mids reads as
    # harsh and fatiguing regardless of how much genuine bandwidth exists.
    out["presence_balance_db"] = float(e_presence - e_lowmid)

    # Midrange scoop: low mids sitting below BOTH their neighbours is the
    # hollow, "smiley-face EQ" character that accompanies tinniness.
    out["midrange_scoop_db"] = float(e_lowmid - (e_bass + e_mid) / 2.0)

    out["band_energy_db"] = {"bass": round(e_bass, 1), "lowmid": round(e_lowmid, 1),
                             "mid": round(e_mid, 1), "presence": round(e_presence, 1)}

    # ── DYNAMICS ─────────────────────────────────────────────────────────────
    peak = float(np.max(np.abs(x)))
    rms  = float(np.sqrt(np.mean(mono ** 2)))
    out["peak_db"] = 20.0 * np.log10(max(peak, 1e-10))
    out["rms_db"]  = 20.0 * np.log10(max(rms, 1e-10))
    out["crest_factor_db"] = out["peak_db"] - out["rms_db"]

    # True peak: 4x oversample (BS.1770-4)
    up = signal.resample_poly(mono, 4, 1)
    out["true_peak_dbtp"] = 20.0 * np.log10(max(float(np.max(np.abs(up))), 1e-10))

    # ── DEFECTS ──────────────────────────────────────────────────────────────
    # Clipping, run-qualified. Isolated full-scale samples are normalisation,
    # not damage; only flat tops of >=10 consecutive samples count.
    at_fs = np.abs(mono) >= 0.999
    out["clipping_pct_raw"] = 100.0 * at_fs.sum() / n
    runs, longest, run = 0, 0, 0
    for v in at_fs:
        if v:
            run += 1
        else:
            if run >= 10:
                runs += run
            longest = max(longest, run)
            run = 0
    if run >= 10:
        runs += run
    longest = max(longest, run)
    out["clipping_pct"] = 100.0 * runs / n
    out["clip_longest_run"] = int(longest)

    # Clicks: LPC(32) prediction residual outliers, per 1-second block.
    # Skipped by default — see COMPUTE_CLICKS.
    out["click_density_per_min"] = _click_density(mono, sr) if COMPUTE_CLICKS else None

    # Channels
    if x.shape[1] >= 2:
        L, R = x[:, 0], x[:, 1]
        rms_l = np.sqrt(np.mean(L ** 2)); rms_r = np.sqrt(np.mean(R ** 2))
        out["channel_balance_db"] = 20.0 * np.log10(max(rms_l, 1e-10) / max(rms_r, 1e-10))
        out["channel_rms_min_db"] = 20.0 * np.log10(max(min(rms_l, rms_r), 1e-10))
        if rms_l > 1e-9 and rms_r > 1e-9:
            out["phase_correlation"] = float(np.corrcoef(L, R)[0, 1])
        else:
            out["phase_correlation"] = 0.0
    else:
        out["channel_balance_db"] = 0.0
        out["channel_rms_min_db"] = out["rms_db"]
        out["phase_correlation"] = 1.0

    # Dropouts: digital silence >20 ms mid-window
    silent = np.abs(mono) < 1e-5
    out["dropout_count"] = _count_runs(silent, int(0.020 * sr))

    # ── HUM: measured in the QUIET frames only ───────────────────────────────
    # Measuring across the whole window counts sustained bass notes as hum —
    # a held low E or B sits right on top of the 60/120 Hz harmonic series the
    # detector is looking for. On the 1979 Danny Gatton recording that inflated
    # the reading from 15.5 dB (real) to 30.3 dB ("strong hum"), which alone
    # dragged its Noise facet down to 38.7.
    #
    # Hum is continuous, so it is fully present in the quiet frames; music is
    # not. Same reasoning as the noise floor above.
    frame_e = P.sum(axis=0)
    quiet_idx = np.where(frame_e <= np.percentile(frame_e, 25))[0]
    hop = nperseg // 2
    segs = [mono[i * hop: i * hop + nperseg] for i in quiet_idx]
    segs = [s for s in segs if len(s) == nperseg]
    quiet_sig = np.concatenate(segs) if segs else mono
    out.update(_hum(quiet_sig if len(quiet_sig) > sr else mono, sr))

    # ── FLAGS: lossy wall detection ──────────────────────────────────────────
    out.update(_lossy(avg_p, freqs))

    # DC offset
    out["dc_offset"] = float(np.mean(mono))

    return out


def _count_runs(mask, min_len):
    """Number of True-runs in `mask` of length >= min_len."""
    if not mask.any():
        return 0
    d = np.diff(np.concatenate(([0], mask.view(np.int8), [0])))
    starts = np.where(d == 1)[0]
    ends = np.where(d == -1)[0]
    return int(np.sum((ends - starts) >= min_len))


def _lpc(x, order):
    """Levinson-Durbin LPC coefficients from autocorrelation."""
    r = np.correlate(x, x, mode="full")[len(x) - 1: len(x) + order]
    if r[0] <= 0:
        return None
    a = np.zeros(order + 1); a[0] = 1.0
    e = r[0]
    for i in range(1, order + 1):
        acc = r[i] + np.dot(a[1:i], r[i - 1:0:-1]) if i > 1 else r[i]
        k = -acc / e
        a[1:i + 1] += k * a[i - 1::-1][:i]
        e *= (1 - k * k)
        if e <= 0:
            return None
    return a


def _click_density(mono, sr, order=32, sigma=20.0, max_width=8):
    """
    Impulsive-defect density (clicks/min) via LPC prediction residual.

    Fit a short-term linear predictor, then look for samples the predictor badly
    mispredicts. Music is highly predictable; a click is not.

    Two refinements that matter enormously in practice:

    * **MAD-based sigma, not std.** The residual distribution is heavy-tailed
      and the outliers we're hunting inflate std, which raises the threshold and
      hides them. Median absolute deviation is robust to exactly that.
    * **Width limit.** A genuine click is a handful of samples. A drum hit or a
      guitar pluck is also unpredictable but produces a *sustained* burst of
      residual. Without a width cap, every snare in the recording reads as a
      click — the first version of this reported 1646 clicks/min on a clean SBD.
    """
    block = sr
    hits = 0
    for s in range(0, max(1, len(mono) - block), block):
        seg = mono[s:s + block]
        if len(seg) < order * 4 or np.max(np.abs(seg)) < 1e-6:
            continue
        seg = seg - seg.mean()
        a = _lpc(seg, order)
        if a is None:
            continue
        res = signal.lfilter(a, [1.0], seg)
        mad = np.median(np.abs(res - np.median(res)))
        sd = mad * 1.4826                     # MAD -> sigma for a normal dist
        if sd <= 0:
            continue
        mask = np.abs(res) > sigma * sd
        if not mask.any():
            continue
        # Count only narrow, isolated bursts
        d = np.diff(np.concatenate(([0], mask.view(np.int8), [0])))
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        widths = ends - starts
        hits += int(np.sum(widths <= max_width))
    minutes = len(mono) / sr / 60.0
    return float(hits / minutes) if minutes > 0 else 0.0


def _hum(mono, sr):
    """
    Mains hum: narrowband lines at 50/60 Hz and harmonics, measured against the
    local spectral median. Decimating to 1 kHz first makes a 0.12 Hz-resolution
    FFT essentially free, which is what separates a stable hum line from bass
    guitar and kick drum energy in the same region.
    """
    y = signal.resample_poly(mono, 10, int(sr / 100))     # -> ~1000 Hz
    fs = 1000.0
    nfft = 8192
    if len(y) < nfft:
        y = np.pad(y, (0, nfft - len(y)))
    f, pxx = signal.welch(y, fs=fs, nperseg=nfft, noverlap=nfft // 2)

    def line_excess(f0):
        excess = []
        for h in range(1, 6):
            fh = f0 * h
            if fh > 450:
                break
            peak_band = (f >= fh - 1.5) & (f <= fh + 1.5)
            local = ((f >= fh - 20) & (f <= fh + 20)) & ~((f >= fh - 3) & (f <= fh + 3))
            if not peak_band.any() or not local.any():
                continue
            excess.append(10 * np.log10(max(pxx[peak_band].max(), 1e-20) /
                                        max(np.median(pxx[local]), 1e-20)))
        return max(excess) if excess else 0.0

    e50, e60 = line_excess(50.0), line_excess(60.0)
    # A recording has one mains frequency; take the weaker reading as the score
    # driver only if it is clearly the wrong family.
    if e50 >= e60:
        return {"hum_ratio_db": float(e50), "hum_mains_hz": 50}
    return {"hum_ratio_db": float(e60), "hum_mains_hz": 60}


def _lossy(avg_p, freqs):
    """
    Lossy-source detection by WALL STEEPNESS, not cutoff frequency.

    An old audience cassette genuinely has no content above ~13 kHz; that is
    the medium, not a transcode. A lossy encoder leaves a cliff: a large drop
    inside a few hundred Hz. Only a steep wall inside 15.5–20.5 kHz counts.
    """
    db = 10 * np.log10(np.maximum(avg_p, 1e-20))
    db = signal.savgol_filter(db, 41, 3) if len(db) > 41 else db
    ref = np.max(db)

    band = (freqs >= 15000) & (freqs <= 21000)
    if not band.any():
        return {"likely_lossy_source": False, "lossy_tier_est": None,
                "lossy_wall_hz": None, "lossy_wall_drop_db": 0.0}

    bf, bd = freqs[band], db[band]
    best_drop, best_hz = 0.0, None
    for i, fq in enumerate(bf):
        j = np.searchsorted(bf, fq + 500)
        if j >= len(bf):
            break
        drop = bd[i] - bd[j]
        if drop > best_drop:
            best_drop, best_hz = float(drop), float(fq)

    # Three conditions, all required. The loose version of this test flagged a
    # 1973 soundboard as a 192k transcode purely on spectral noise.
    #   1. a genuinely steep wall (>=35 dB inside 500 Hz)
    #   2. everything above the wall is *dead*, not merely quiet (<-85 dB rel peak)
    #   3. the dead region is flat — a real encoder floor, not a continuing slope
    is_lossy = False
    if best_hz is not None and best_drop >= 35.0:
        above = freqs > best_hz + 800
        if above.sum() >= 8:
            above_db = db[above]
            dead = (np.mean(above_db) - ref) < -85.0
            flat = (np.max(above_db) - np.min(above_db)) < 12.0
            is_lossy = bool(dead and flat)

    tier = None
    if is_lossy:
        for lim, name in ((16500, "MP3 128k"), (19000, "MP3 192k / V2"),
                          (19800, "MP3 256k"), (21000, "MP3 320k / V0")):
            if best_hz <= lim:
                tier = name
                break
    return {"likely_lossy_source": is_lossy, "lossy_tier_est": tier,
            "lossy_wall_hz": best_hz, "lossy_wall_drop_db": best_drop}


# ═════════════════════════════════════════════════════════════════════════════
# Whole-file features (cheap, no windowing needed)
# ═════════════════════════════════════════════════════════════════════════════
def effective_bit_depth(path, claimed):
    """
    Measured, not claimed. Finds how many low-order bits are actually in use —
    a 24-bit file padded up from 16 has its bottom 8 bits permanently zero.
    """
    try:
        with sf.SoundFile(path) as f:
            sr = f.samplerate
            f.seek(int(min(60, max(0, f.frames / sr - 30)) * sr))
            x = f.read(int(10 * sr), dtype="int32", always_2d=True)
    except Exception:
        return claimed
    if x.size == 0:
        return claimed
    v = x[x != 0]
    if v.size == 0:
        return claimed
    # soundfile left-justifies into int32; count trailing zero bits
    trailing = 32
    for shift in range(32):
        if np.any((v & (1 << shift)) != 0):
            trailing = shift
            break
    return max(1, 32 - trailing)


WAVEFORM_POINTS = 800


def waveform_peaks(path, n=WAVEFORM_POINTS):
    """
    Min/max peak envelope across the whole track, for the preview player.

    Streams the file in n blocks and keeps each block's true min and max rather
    than an RMS average — a bipolar peak envelope is what actually looks like a
    waveform, where a mirrored RMS curve looks smooth and lifeless.

    Cheap enough to do during analysis: measured at ~390x realtime, so about
    2 s for a 13-minute track. Values are quantised to integers in -100..100,
    which keeps the JSON payload around 8 kB per track and is well beyond the
    resolution a small player can show.
    """
    try:
        with sf.SoundFile(path) as f:
            total = f.frames
            if not total:
                return None
            per = max(1, total // n)
            lo, hi = [], []
            for _ in range(n):
                blk = f.read(per, dtype="float32", always_2d=True)
                if not len(blk):
                    break
                m = blk.mean(axis=1)
                lo.append(float(m.min()))
                hi.append(float(m.max()))
    except Exception:
        return None
    if not hi:
        return None
    norm = max(max(abs(v) for v in hi), max(abs(v) for v in lo), 1e-6)
    return {"min": [int(round(v / norm * 100)) for v in lo],
            "max": [int(round(v / norm * 100)) for v in hi]}


def loudness_lufs(path, offsets):
    """Integrated LUFS (ITU-R BS.1770) over the sampled windows, concatenated."""
    try:
        import pyloudnorm as pyln
    except ImportError:
        return None
    chunks, sr = [], None
    for off in offsets:
        x, sr = read_window(path, off)
        chunks.append(x)
    if not chunks:
        return None
    y = np.concatenate(chunks, axis=0)
    try:
        return float(pyln.Meter(sr).integrated_loudness(y))
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
# Recording-level extraction
# ═════════════════════════════════════════════════════════════════════════════
# Stable features take the median across windows; defect features take the max
# (a defect present in one window is present).
_MEDIAN_KEYS = ["noise_floor_468_db", "program_468_db", "snr_468_db", "hiss_db",
                "hf_snr_db", "mid_snr_db", "rumble_db", "rolloff_95_hz", "hf_edge_hz",
                "hf_energy_ratio_db", "presence_balance_db", "midrange_scoop_db",
                "spectral_tilt_db_oct", "crest_factor_db", "peak_db", "rms_db",
                "channel_balance_db", "phase_correlation", "hum_ratio_db",
                "dc_offset", "channel_rms_min_db",
                # Audience/room measures added 2026-07-31 — see analyse_window.
                "crowd_snr_db", "noise_nonstationarity_db", "modulation_index"]
_MAX_KEYS    = ["clipping_pct", "clipping_pct_raw", "clip_longest_run",
                "dropout_count", "true_peak_dbtp", "lossy_wall_drop_db"]
# Click density is a RATE, so the robust estimator across windows is the median,
# not the max. Taking the max let a single pathological window (11223/min on
# Plummer Hall) wipe out an otherwise excellent recording.
_MEDIAN_KEYS.append("click_density_per_min")


def extract_recording_features(folder, non_music_paths=None):
    """Full raw-feature extraction for one recording folder."""
    picks, note = select_tracks(folder)
    if not picks:
        return {"error": note}

    per_window, sampled = [], []
    for path, duration in picks:
        offsets, used = window_offsets(duration), []
        for off in offsets:
            x, sr = read_window(path, off)
            if len(x) < sr:          # window ran off the end
                continue
            # Skip near-silent windows — an intro gap, a fade, or a stretch of
            # room tone tells us nothing about the recording's quality and
            # would drag the noise measurement around.
            rms = float(np.sqrt(np.mean(x.mean(axis=1) ** 2)))
            if 20 * np.log10(max(rms, 1e-10)) < SILENT_RMS_DB:
                continue
            per_window.append(analyse_window(x, sr))
            used.append(round(off, 1))
        # `rel` (added 2026-07-30) is what the UI streams with: the preview
        # endpoint joins folder + this path, and a bare basename does not
        # resolve for an un-flattened recording whose audio sits in CD1/.
        # `track` stays the basename because it is what gets displayed.
        sampled.append({"track": os.path.basename(path),
                        "rel": os.path.relpath(path, folder),
                        "duration_s": round(duration, 1),
                        "offsets": used})
    if not per_window:
        return {"error": "no usable windows"}

    feats = {}
    for k in _MEDIAN_KEYS:
        vals = [w[k] for w in per_window if k in w and w[k] is not None]
        if vals:
            feats[k] = float(np.median(vals))
    for k in _MAX_KEYS:
        vals = [w[k] for w in per_window if k in w and w[k] is not None]
        if vals:
            feats[k] = float(np.max(vals))

    feats["likely_lossy_source"] = any(w.get("likely_lossy_source") for w in per_window)
    tiers = [w.get("lossy_tier_est") for w in per_window if w.get("lossy_tier_est")]
    feats["lossy_tier_est"] = tiers[0] if tiers else None
    # Where the steepest high-frequency wall sits. Needed for display: the
    # cutoff number alone means nothing without the steepness that decides
    # whether it is an encoder wall or an analogue rolloff.
    walls = [w.get("lossy_wall_hz") for w in per_window if w.get("lossy_wall_hz")]
    feats["lossy_wall_hz"] = float(np.median(walls)) if walls else None
    # MODE, not median. A recording has one mains frequency — it is 50 or 60,
    # never anything between. Taking the median of per-window readings produced
    # a literal 55 Hz whenever the windows split evenly, which is not a
    # frequency that exists.
    _mains = [w["hum_mains_hz"] for w in per_window if w.get("hum_mains_hz")]
    feats["hum_mains_hz"] = (max(set(_mains), key=_mains.count) if _mains else None)
    # How consistently the windows agreed. Used to decide whether the reading
    # is worth reporting at all.
    feats["hum_mains_agreement"] = (round(_mains.count(feats["hum_mains_hz"]) / len(_mains), 2)
                                    if _mains else None)

    path = picks[0][0]
    info = sf.info(path)
    claimed = {"PCM_16": 16, "PCM_24": 24, "PCM_32": 32,
               "PCM_S8": 8, "PCM_U8": 8}.get(info.subtype, 16)
    eff = effective_bit_depth(path, claimed)

    # ── Container + real bitrate ─────────────────────────────────────────────
    # Format comes from the extension rather than libsndfile's name, because
    # what a collector wants to see is "FLAC" / "SHN" / "MP3", not "FLAC
    # (Free Lossless Audio Codec)". SHN isn't decodable here but should still
    # be reported honestly rather than shown as unknown.
    ext = os.path.splitext(path)[1].lstrip(".").upper()
    feats["format"] = {"FLAC": "FLAC", "SHN": "SHN", "MP3": "MP3",
                       "WAV": "WAV", "AIFF": "AIFF", "AIF": "AIFF",
                       "M4A": "ALAC/AAC", "APE": "APE",
                       "WV": "WavPack"}.get(ext, ext or "unknown")

    # Actual encoded bitrate across the sampled tracks — file bytes over
    # duration. For a lossless container this is the real compressed rate, and
    # a suspiciously low one is a hint the source had less information in it
    # than the container implies.
    tot_bytes = tot_secs = 0
    for p, dur in picks:
        try:
            tot_bytes += os.path.getsize(p)
            tot_secs += dur
        except OSError:
            continue
    feats["bitrate_kbps"] = (round(tot_bytes * 8 / tot_secs / 1000)
                             if tot_secs > 0 else None)
    feats["claimed_bit_depth"]   = claimed
    feats["effective_bit_depth"] = min(eff, claimed)
    feats["bit_depth_padded"]    = bool(eff < claimed - 2)
    feats["sample_rate_hz"]      = info.samplerate
    feats["channels"]            = info.channels

    # Upsampling: claims a high rate but carries no content near its own Nyquist
    nyq = info.samplerate / 2.0
    feats["upsampled"] = bool(nyq > 24000 and
                              feats.get("rolloff_85_hz", 0) < 20000 and
                              feats.get("hf_energy_ratio_db", -99) < -60)

    feats["lufs_integrated"] = loudness_lufs(path, window_offsets(picks[0][1]))

    feats["sampled"]        = sampled
    feats["sampled_track"]  = ", ".join(s["track"] for s in sampled)
    feats["selection_note"] = note
    feats["analysis_version"]   = QUALITY_ANALYSIS_VERSION
    return feats


if __name__ == "__main__":
    import sys
    print(json.dumps(extract_recording_features(sys.argv[1]), indent=2, default=str))
