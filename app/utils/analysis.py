"""
utils/analysis.py — Per-track audio analysis via Librosa.

Called by the reprocess endpoint. Each track is analysed independently;
results are upserted into the track_analysis table.

Metrics produced
────────────────
  rms_db               Average RMS level (dBFS)
  peak_db              True peak (dBFS)
  noise_floor_db       Estimated noise floor — 5th-percentile RMS frame (dBFS)
  dynamic_range_db     peak_db − noise_floor_db
  clipping_pct         Percentage of samples at full scale (|x| >= 0.999)
  dc_offset            Mean sample value (ideal = 0.0)
  spectral_centroid_hz Average spectral centroid (Hz) — perceptual brightness
  waveform_json        JSON {"min": [...], "max": [...]} — true per-bucket peak
                       envelope (signed, -1.0-1.0) at WAVEFORM_POINTS resolution,
                       covering the full track duration, for waveform display.
                       (v1 stored a single mirrored RMS-magnitude array instead —
                       the frontend still renders that shape for tracks that
                       haven't been re-analysed since the v2 bump.)
"""

import json
import logging
import numpy as np

log = logging.getLogger(__name__)

# "3" (2026-08-28): BPM removed. Bumped so analyse_and_store_track's
# reanalyze=False skip-path re-runs stale rows rather than leaving a library
# half on the old shape — the same version gate the quality engine uses.
ANALYSIS_VERSION = "3"

# Marker written by the ingest's cheap non-music pass (api/ingest.py::
# _store_non_music_signal) on rows that hold ONLY spectral_flatness,
# duration_s and non_music_score. Lives here, beside ANALYSIS_VERSION, because
# three modules need it — the ingest that writes it and the two serialisers
# that must not mistake a partial for a finished analysis. Any value other than
# ANALYSIS_VERSION makes analyse_and_store_track re-run the full pass, which is
# exactly what should happen to a partial.
SIGNALS_ONLY_VERSION = "signals"
WAVEFORM_POINTS  = 2000  # resolution of waveform envelope — was 300 (v1)


def _to_db(amplitude, min_db=-80.0):
    """Convert linear amplitude to dBFS, clamped to min_db."""
    if amplitude <= 0:
        return min_db
    return float(np.clip(20.0 * np.log10(amplitude), min_db, 0.0))


def analyse_track(abs_path):
    """
    Load a FLAC (or any soundfile-compatible format) and return a dict of
    analysis metrics. Returns None and logs a warning on failure.

    Uses sr=None to preserve the native sample rate (avoids costly resample).
    mono=True collapses channels for RMS/spectral analysis.
    """
    try:
        import librosa
    except ImportError:
        log.error("librosa not installed — pip install librosa soundfile")
        return None

    # ── Header metadata (mutagen — no decode) ────────────────────────────────
    sample_rate_hz = None
    bit_depth      = None
    bitrate_kbps   = None
    try:
        import mutagen
        mf = mutagen.File(abs_path)
        if mf and hasattr(mf, 'info'):
            info = mf.info
            sample_rate_hz = getattr(info, 'sample_rate', None)
            bit_depth      = getattr(info, 'bits_per_sample', None)
            # For lossy formats mutagen gives bitrate in bps
            br = getattr(info, 'bitrate', None)
            if br and not bit_depth:
                bitrate_kbps = round(br / 1000)
    except Exception as e:
        log.warning("mutagen header read failed for %s: %s", abs_path, e)

    try:
        # Load at native SR, mono for analysis
        y, sr = librosa.load(abs_path, sr=None, mono=True)
    except Exception as e:
        log.warning("Could not load %s: %s", abs_path, e)
        return None

    # sr from librosa is authoritative (int); use as fallback if mutagen missed it
    if not sample_rate_hz and sr:
        sample_rate_hz = int(sr)

    if len(y) == 0:
        log.warning("Empty audio: %s", abs_path)
        return None

    # ── RMS & peak ────────────────────────────────────────────────────────────
    # Frame-level RMS (hop ~23 ms at 44.1 kHz) gives us both per-frame detail
    # and a rolling view for the waveform envelope.
    hop_length = 1024
    frame_rms  = librosa.feature.rms(y=y, hop_length=hop_length)[0]  # shape: (n_frames,)

    rms_mean   = float(np.mean(frame_rms))
    peak_amp   = float(np.max(np.abs(y)))
    rms_db     = _to_db(rms_mean)
    peak_db    = _to_db(peak_amp)

    # ── Noise floor ───────────────────────────────────────────────────────────
    # 5th-percentile frame RMS — quietest sustained sections ≈ noise floor
    noise_amp      = float(np.percentile(frame_rms, 5))
    noise_floor_db = _to_db(noise_amp)
    dynamic_range  = round(peak_db - noise_floor_db, 1)

    # ── Clipping ──────────────────────────────────────────────────────────────
    clipped     = np.sum(np.abs(y) >= 0.999)
    clipping_pct = round(float(clipped) / len(y) * 100.0, 4)

    # ── DC offset ─────────────────────────────────────────────────────────────
    dc_offset = round(float(np.mean(y)), 6)

    # ── Spectral analysis (STFT shared by centroid + cutoff) ─────────────────
    # n_fft=4096 gives ~10 Hz bin resolution at 44.1 kHz — fine enough to
    # pinpoint the MP3 brick-wall cutoff without being too slow.
    S = np.abs(librosa.stft(y, n_fft=4096)) ** 2   # power spectrogram
    freqs = librosa.fft_frequencies(sr=sr, n_fft=4096)

    # Centroid (perceptual brightness)
    centroid = librosa.feature.spectral_centroid(S=S, sr=sr)
    spectral_centroid_hz = round(float(np.mean(centroid)), 1)

    # ── Spectral flatness — the non-music signal (2026-08-28) ────────────────
    # Wiener entropy: how evenly energy is spread across the spectrum. Speech
    # and applause are flat; sustained instrumental tones are peaky. Free here
    # because S is already computed, and the raw value is stored rather than a
    # verdict — the verdict needs the whole recording, since the useful
    # quantity is this track's flatness RELATIVE to its own show's median.
    # See utils/track_signals.py for why absolute thresholds do not work.
    #
    # spectral_flatness() wants MAGNITUDE; S here is power (already squared),
    # so take the root back out rather than handing it the wrong quantity.
    spectral_flatness = float(np.median(
        librosa.feature.spectral_flatness(S=np.sqrt(S))))

    # ── Spectral cutoff ───────────────────────────────────────────────────────
    # Average power spectrum across time, then find the highest frequency bin
    # still carrying meaningful energy (> −40 dB relative to spectral peak).
    # A hard wall well below Nyquist is the classic lossy-transcode fingerprint.
    avg_power   = S.mean(axis=1)                       # shape: (n_fft/2+1,)
    peak_power  = avg_power.max()
    threshold   = peak_power * (10 ** (-40.0 / 10.0)) # −40 dB
    active_bins = np.where(avg_power > threshold)[0]
    spectral_cutoff_hz = round(float(freqs[active_bins[-1]])) if len(active_bins) else None

    # ── BPM — REMOVED 2026-08-28 (Ryan: "BPM is meaningless, remove") ─────────
    # It was also, by a wide margin, the most expensive thing in this function:
    # librosa.beat.beat_track measured 29.1 s on a single 4.6-minute 24/96
    # track, against 57.7 s for the whole of analyse_track. Half the cost of
    # every track analysis in the library was buying a number that does not
    # mean anything for this material — a 17-minute improvised jam does not
    # have "a tempo", and neither does two minutes of a band being introduced.
    # The column stays on track_analysis for now so old rows are readable;
    # nothing writes it any more.

    # ── Waveform envelope (signed min/max peaks, -1..1) ───────────────────────
    # Split the raw signal itself (not the already-smoothed frame_rms) into
    # WAVEFORM_POINTS buckets and keep each bucket's true min and max sample —
    # a real bipolar peak envelope rather than a mirrored RMS average. This is
    # what actually looks "punchy" instead of smooth/rounded.
    n = len(y)
    if n >= WAVEFORM_POINTS:
        buckets = np.array_split(y, WAVEFORM_POINTS)
        wf_min = np.array([b.min() for b in buckets])
        wf_max = np.array([b.max() for b in buckets])
    else:
        pad = np.zeros(WAVEFORM_POINTS - n)
        wf_min = np.concatenate([y, pad])
        wf_max = np.concatenate([y, pad])

    norm = peak_amp or 1.0
    waveform = {
        "min": [round(float(v) / norm, 4) for v in wf_min],
        "max": [round(float(v) / norm, 4) for v in wf_max],
    }

    return {
        "sample_rate_hz":       sample_rate_hz,
        "bit_depth":            bit_depth,
        "bitrate_kbps":         bitrate_kbps,
        "rms_db":               round(rms_db, 1),
        "peak_db":              round(peak_db, 1),
        "noise_floor_db":       round(noise_floor_db, 1),
        "dynamic_range_db":     dynamic_range,
        "clipping_pct":         clipping_pct,
        "dc_offset":            dc_offset,
        "spectral_centroid_hz": spectral_centroid_hz,
        "spectral_cutoff_hz":   spectral_cutoff_hz,
        "spectral_flatness":    round(spectral_flatness, 6),
        "duration_s":           round(len(y) / float(sr), 2),
        "waveform_json":        json.dumps(waveform),
        "analysis_version":     ANALYSIS_VERSION,
    }


def store_track_analysis(track, result, db_session, *, commit=True):
    """
    Upsert one analyse_track() result onto the track's `track_analysis` row.

    Extracted 2026-08-07. This block previously existed in three places —
    `analyse_recording()` below, `scripts/batch_analyze.py`, and (nearly) the
    quality backfill — which is exactly the drift risk the project's
    define-once rule exists to prevent: `spectral_cutoff_hz` was added to the
    schema and one copy could easily have missed it.

    Commits per track by default: long analysis runs must not hold a SQLite
    write lock open across the whole batch.
    """
    from app.models.track_analysis import TrackAnalysis
    from datetime import datetime, timezone

    ta = db_session.query(TrackAnalysis).filter_by(track_id=track.id).first()
    if ta is None:
        ta = TrackAnalysis(track_id=track.id)
        db_session.add(ta)

    for col in ("sample_rate_hz", "bit_depth", "bitrate_kbps", "rms_db",
                "peak_db", "noise_floor_db", "dynamic_range_db",
                "clipping_pct", "dc_offset", "spectral_centroid_hz",
                "spectral_cutoff_hz", "spectral_flatness", "duration_s",
                "waveform_json",
                "analysis_version"):
        setattr(ta, col, result[col])
    ta.analyzed_at = datetime.now(timezone.utc)

    if commit:
        db_session.commit()
    return ta


def analyse_and_store_track(track, abs_path, db_session, *, reanalyze=False):
    """
    Analyse one track and store the result. Returns a status string:

        "skipped" — already has a current-version row (and reanalyze is off)
        "missing" — the file is not on disk
        "failed"  — decode/analysis returned nothing
        "ok"      — analysed and stored

    Status strings rather than exceptions because every caller is a long batch
    loop that must continue past a single bad file and report a tally at the
    end.
    """
    import os
    from app.models.track_analysis import TrackAnalysis

    if not reanalyze:
        existing = (db_session.query(TrackAnalysis)
                    .filter_by(track_id=track.id,
                               analysis_version=ANALYSIS_VERSION)
                    .first())
        if existing is not None:
            return "skipped"

    if not os.path.isfile(abs_path):
        return "missing"

    result = analyse_track(abs_path)
    if result is None:
        return "failed"

    store_track_analysis(track, result, db_session)
    return "ok"


def analyse_recording(recording, library_root, db_session, *, reanalyze=True):
    """
    Run analyse_track() on every track in a Recording and upsert results
    into the track_analysis table. Returns (n_ok, errors).

    `reanalyze` defaults True to preserve this function's original behaviour —
    it is the "Re-analyze" button's implementation, where an unconditional
    redo is the whole point. Batch callers pass False.
    """
    n_ok   = 0
    errors = []

    for track in recording.tracks:
        abs_path = f"{library_root}/{recording.folder_path}/{track.file_path}"
        log.info("Analysing %s", abs_path)

        status = analyse_and_store_track(track, abs_path, db_session,
                                         reanalyze=reanalyze)
        if status == "ok":
            n_ok += 1
        elif status == "skipped":
            continue
        else:
            errors.append((track.file_path,
                           "File missing" if status == "missing"
                           else "Analysis failed"))

    # Recording-level pass. MUST run here and not inside analyse_and_store_track:
    # the non-music score is a comparison against the OTHER tracks of this same
    # recording, so it cannot exist until every track has a flatness reading.
    # Runs over whatever is stored — including tracks skipped as already-current
    # — so a re-analysis of one track still scores against the full show.
    score_recording_non_music(recording, db_session)

    return n_ok, errors


def score_recording_non_music(recording, db_session, *, commit=True):
    """
    Fill in every track's `non_music_score` for one recording.

    Separate and public because it is cheap (pure arithmetic over stored
    numbers — no audio, no filesystem) and therefore worth being able to re-run
    across the library after a threshold change, exactly as the quality engine's
    rescore_stored() does for its own curves.

    A recording that cannot be scored has its tracks set back to NULL rather
    than left holding a stale number from a previous shape — a score computed
    against a different set of tracks is worse than no score.
    """
    from app.models.track_analysis import TrackAnalysis
    from app.utils.track_signals import non_music_scores

    rows = (db_session.query(TrackAnalysis)
            .filter(TrackAnalysis.track_id.in_([t.id for t in recording.tracks]))
            .all()) if recording.tracks else []
    if not rows:
        return {}

    scored = non_music_scores(
        (r.track_id, r.spectral_flatness, r.duration_s) for r in rows)
    for r in rows:
        hit = scored.get(r.track_id)
        r.non_music_score = hit["score"] if hit else None
    if commit:
        db_session.commit()
    return scored
