"""
models/track_analysis.py — Per-track Librosa analysis results.

One row per track, replaced on each reprocess run.
Waveform data stored as a JSON array of ~300 RMS values (normalised 0-1),
suitable for direct rendering by the frontend canvas.
"""

from app.extensions import db


class TrackAnalysis(db.Model):
    __tablename__ = "track_analysis"

    id          = db.Column(db.Integer, primary_key=True)
    track_id    = db.Column(db.Integer, db.ForeignKey("track.id", ondelete="CASCADE"),
                            nullable=False, unique=True, index=True)

    # Level metrics (all in dBFS)
    rms_db           = db.Column(db.Float, nullable=True)   # average RMS level
    peak_db          = db.Column(db.Float, nullable=True)   # true peak
    noise_floor_db   = db.Column(db.Float, nullable=True)   # estimated noise floor
    dynamic_range_db = db.Column(db.Float, nullable=True)   # peak minus noise floor

    # Format / encoding (from file header — no decode needed)
    sample_rate_hz   = db.Column(db.Integer, nullable=True)  # e.g. 44100, 48000, 96000
    bit_depth        = db.Column(db.Integer, nullable=True)  # e.g. 16, 24 (lossless)
    bitrate_kbps     = db.Column(db.Integer, nullable=True)  # e.g. 320 (lossy only)

    # Quality signals
    clipping_pct     = db.Column(db.Float, nullable=True)   # % of samples at ±1.0
    dc_offset        = db.Column(db.Float, nullable=True)   # mean sample value

    # Spectral / musical
    spectral_centroid_hz = db.Column(db.Float, nullable=True)  # brightness
    spectral_cutoff_hz   = db.Column(db.Integer, nullable=True) # highest active freq — lossy transcodes show hard wall below Nyquist
    # ── Non-music detection (2026-08-28) ─────────────────────────────────────
    # `spectral_flatness` is the raw per-track measurement — Wiener entropy,
    # how evenly energy is spread across the spectrum. `duration_s` is stored
    # beside it because the score needs both, and reading it back off disk per
    # track just to compare durations would be silly.
    #
    # `non_music_score` (0-1) is DERIVED, and derived at the RECORDING level:
    # it is this track's flatness and length measured against the medians of
    # its own show. A track's own numbers cannot produce it — see
    # utils/track_signals.py, which documents why absolute thresholds fail.
    # NULL means "not scored": too few tracks in the recording, or analysis
    # never ran (Quick Add defers it).
    spectral_flatness    = db.Column(db.Float, nullable=True)
    duration_s           = db.Column(db.Float, nullable=True)
    non_music_score      = db.Column(db.Float, nullable=True)

    # Written by nothing since 2026-08-28 (BPM removed — meaningless for live
    # concert material, and beat_track was half the cost of analysing a track).
    # Column kept so existing rows still read; drop it in a future migration.
    bpm                  = db.Column(db.Float, nullable=True)   # deprecated

    # Waveform envelope — JSON list of ~300 floats, 0.0–1.0
    waveform_json    = db.Column(db.Text, nullable=True)

    # Bookkeeping
    analysis_version = db.Column(db.String(16), nullable=False, default="1")
    analyzed_at      = db.Column(db.DateTime(timezone=True),
                                  server_default=db.func.now(), nullable=False)

    # Relationship back to track
    track = db.relationship("Track", backref=db.backref("analysis", uselist=False))
