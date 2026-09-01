"""
api/tracks.py — Track endpoints.

Routes:
  GET  /api/tracks/<id>              — track detail
  PUT  /api/tracks/<id>              — update track metadata; renames file when title changes
  GET  /api/tracks/<id>/spectrogram  — linear-frequency spectrogram PNG
  POST /api/tracks/<id>/play         — log a play event
"""

import io
import os
import json
import logging
import traceback
from flask import Blueprint, jsonify, request, current_app, send_file
from flask_login import login_required, current_user
from datetime import datetime, timezone

# Set matplotlib backend before any pyplot import — must happen at module load time
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    _MPL_OK = True
except Exception as _mpl_err:
    logging.getLogger(__name__).warning("matplotlib unavailable: %s", _mpl_err)
    _MPL_OK = False

from app.extensions import db
from app.models.track import Track
from app.models.play_log import PlayLog
from app.utils.folder_naming import _sanitize, unique_file_name

bp  = Blueprint("tracks", __name__)
log = logging.getLogger(__name__)


def _build_track_filename(track_number, title, old_file_path):
    """
    Build a canonical filename for a track given its number and title.

    Format: {track_number:02d} - {sanitized_title}.{ext}

    Preserves the original file extension and any leading directory component
    in old_file_path (e.g. "disc1/01 - Dark Star.flac" stays under "disc1/").
    """
    ext      = os.path.splitext(old_file_path)[1] or '.flac'
    subdir   = os.path.dirname(old_file_path)           # '' for flat layouts
    stem     = f"{track_number:02d} - {_sanitize(title)}"
    filename = stem + ext
    return os.path.join(subdir, filename) if subdir else filename


@bp.route("/<int:track_id>")
@login_required
def get_track(track_id):
    t = db.session.get(Track, track_id)
    if not t:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id":           t.id,
        "recording_id": t.recording_id,
        "track_number": t.track_number,
        "title":        t.title,
        "set_number":   t.set_number,
        "duration":     t.duration,
        "is_official":  bool(t.is_official),
        "flags":        json.loads(t.flags) if t.flags else [],
        "songwriter":   t.songwriter,
        "notes":        t.notes,
        "stream_url":   f"/api/stream/{t.id}",
    })


@bp.route("/<int:track_id>", methods=["PUT"])
@login_required
def update_track(track_id):
    t = db.session.get(Track, track_id)
    if not t:
        return jsonify({"error": "Not found"}), 404

    data          = request.get_json()
    rename_warning = None

    # ── File rename when title changes ────────────────────────────────────────
    new_title = data.get("title", "").strip() or None
    if new_title and new_title != t.title:
        library_root  = current_app.config.get("LIBRARY_ROOT", "")
        rec           = t.recording
        new_file_path = _build_track_filename(t.track_number, new_title, t.file_path)

        old_abs = os.path.join(library_root, rec.folder_path, t.file_path)
        new_abs = os.path.join(library_root, rec.folder_path, new_file_path)

        if new_abs != old_abs:
            if os.path.exists(old_abs):
                # Never let a retitle destroy a sibling track's audio.
                # os.rename() REPLACES an existing destination file silently
                # (a directory at least errors), and two tracks can reach the
                # same filename honestly — same number + same title, or any
                # folder that ended up holding two shows, both numbered from
                # 01. Dedupe with the same "(2)" convention used for folder
                # names and for within-batch collisions at ingest.
                # 2026-09-01, alongside the ingest folder-merge fix.
                subdir        = os.path.dirname(new_file_path)
                parent_abs    = os.path.join(library_root, rec.folder_path, subdir)
                unique        = unique_file_name(parent_abs,
                                                 os.path.basename(new_file_path),
                                                 keep_abs=old_abs)
                new_file_path = os.path.join(subdir, unique) if subdir else unique
                new_abs       = os.path.join(library_root, rec.folder_path, new_file_path)
                try:
                    os.rename(old_abs, new_abs)
                    t.file_path = new_file_path
                    log.info("Renamed track file: %s → %s", old_abs, new_abs)
                except OSError as e:
                    rename_warning = f"Title saved but file rename failed: {e}"
                    log.warning("Track file rename failed: %s", e)
            else:
                rename_warning = f"File not found on disk, only DB title updated ({t.file_path})"
                log.warning("Track file not found for rename: %s", old_abs)

    # ── DB field updates ──────────────────────────────────────────────────────
    for field in ["title", "set_number", "notes", "songwriter"]:
        if field in data:
            setattr(t, field, data[field] or None)

    if "is_official" in data:
        t.is_official = bool(data["is_official"])

    if "flags" in data:
        flags = data["flags"]
        if isinstance(flags, list):
            t.flags = json.dumps(flags) if flags else None
        elif isinstance(flags, str):
            t.flags = flags or None
        else:
            t.flags = None

    db.session.commit()

    resp = {"id": t.id, "file_path": t.file_path}
    if rename_warning:
        resp["warning"] = rename_warning
    return jsonify(resp)


@bp.route("/<int:track_id>/spectrogram")
@login_required
def track_spectrogram(track_id):
    """
    GET /api/tracks/<id>/spectrogram
    Linear-frequency spectrogram PNG. Hard cutoff from lossy transcodes is
    immediately visible as a flat ceiling well below Nyquist.
    Uses the first 90 s for speed.
    """
    if not _MPL_OK:
        return jsonify({"error": "matplotlib unavailable"}), 500

    t = db.session.get(Track, track_id)
    if not t:
        return jsonify({"error": "Not found"}), 404

    library_root = str(current_app.config["LIBRARY_ROOT"])
    # Eagerly grab the relationship values while session is open
    folder_path  = t.recording.folder_path
    abs_path     = os.path.join(library_root, folder_path, t.file_path)

    if not os.path.exists(abs_path):
        return jsonify({"error": f"File not found: {abs_path}"}), 404

    try:
        import numpy as np
        import librosa

        y, sr = librosa.load(abs_path, sr=None, mono=True, duration=90)

        n_fft = 4096
        hop   = 512
        D     = librosa.amplitude_to_db(
                    np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop)),
                    ref=np.max)
        freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
        times = librosa.frames_to_time(np.arange(D.shape[1]), sr=sr, hop_length=hop)

        fig, ax = plt.subplots(figsize=(6, 2.5), dpi=120)
        fig.patch.set_facecolor('#0c0f14')
        ax.set_facecolor('#0c0f14')

        ax.pcolormesh(times, freqs / 1000, D,
                      shading='auto', cmap='magma', vmin=-80, vmax=0)

        ax.set_xlabel('Time (s)', color='#8a7a68', fontsize=8)
        # kHz labels on the right so the left edge of the plot aligns with the waveform
        ax.set_ylabel('kHz', color='#8a7a68', fontsize=8, labelpad=4)
        ax.yaxis.set_label_position('right')
        ax.yaxis.tick_right()
        ax.tick_params(colors='#8a7a68', labelsize=7)
        for spine in ax.spines.values():
            spine.set_edgecolor('#2a2520')

        ax.set_ylim(0, sr / 2000)   # 0 → Nyquist in kHz
        ax.yaxis.set_major_formatter(ticker.FormatStrFormatter('%.0f'))
        plt.tight_layout(pad=0.4)
        # Eliminate residual left margin so the plot edge aligns with the waveform canvas
        fig.subplots_adjust(left=0.0)

        buf = io.BytesIO()
        fig.savefig(buf, format='png', facecolor='#0c0f14')
        plt.close(fig)
        buf.seek(0)
        return send_file(buf, mimetype='image/png')

    except Exception:
        log.exception("Spectrogram generation failed for track %s", track_id)
        return jsonify({"error": traceback.format_exc()}), 500


@bp.route("/<int:track_id>/play", methods=["POST"])
@login_required
def log_play(track_id):
    """Called by the frontend when a track finishes or is stopped."""
    data  = request.get_json() or {}
    entry = PlayLog(
        user_id         = current_user.id,
        track_id        = track_id,
        played_at       = datetime.now(timezone.utc),
        duration_played = data.get("duration_played"),
        completed       = data.get("completed", False),
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify({"ok": True}), 201
