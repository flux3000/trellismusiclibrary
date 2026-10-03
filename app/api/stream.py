"""
api/stream.py — Audio streaming endpoint.

Serves FLAC audio by track ID. The actual file path is resolved server-side
from the database and LIBRARY_ROOT — it is never exposed to the frontend.

Supports HTTP Range requests so the browser audio player can seek.

Route:
  GET /api/stream/<int:track_id>
"""

import os
from flask import Blueprint, current_app, request, Response, abort
from flask_login import login_required
from app.extensions import db
from app.models.track import Track
from app.models.recording import Recording
from app.api.system import require_library
from app.utils.paths import within_import_roots

bp = Blueprint("stream", __name__)

CHUNK_SIZE = 1024 * 256  # 256 KB chunks
MIMETYPE   = "audio/flac"


def _path_within(candidate, base):
    """
    True if the resolved `candidate` path is equal to, or nested inside,
    the resolved `base` directory. Both inputs should already be
    os.path.realpath()'d by the caller — this only does the string
    comparison, shared by every containment check in this module.
    """
    return candidate == base or candidate.startswith(base + os.sep)


def _serve_file(full_path, mimetype=MIMETYPE):
    """
    Stream a file with HTTP Range support (enables seeking): a 206 partial
    response when a Range header is present, else a 200 full-file stream.
    Single implementation shared by the local FLAC endpoints AND the peer
    transcoded-MP3 endpoint (api/share.py), which passes mimetype="audio/mpeg".
    """
    file_size    = os.path.getsize(full_path)
    range_header = request.headers.get("Range")

    if range_header:
        byte_start, byte_end = _parse_range(range_header, file_size)
        length = byte_end - byte_start + 1

        def generate_range():
            with open(full_path, "rb") as f:
                f.seek(byte_start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return Response(generate_range(), status=206, headers={
            "Content-Range":  f"bytes {byte_start}-{byte_end}/{file_size}",
            "Accept-Ranges":  "bytes",
            "Content-Length": str(length),
            "Content-Type":   mimetype,
        })

    def generate_full():
        with open(full_path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                yield chunk

    return Response(generate_full(), status=200, headers={
        "Content-Length": str(file_size),
        "Accept-Ranges":  "bytes",
        "Content-Type":   mimetype,
    })


@bp.route("/<int:track_id>")
@login_required
@require_library()
def stream_track(track_id):
    # Resolve track → recording → full filesystem path
    track = db.session.get(Track, track_id)
    if not track:
        abort(404)

    recording = db.session.get(Recording, track.recording_id)
    if not recording:
        abort(404)

    library_root      = current_app.config["LIBRARY_ROOT"]
    real_library_root = os.path.realpath(library_root)
    full_path         = os.path.realpath(
        os.path.join(library_root, recording.folder_path, track.file_path)
    )

    # Defense in depth: folder_path/file_path come from the DB, not directly
    # from the request, but a corrupted or maliciously-written row should
    # still not be able to serve a file outside LIBRARY_ROOT.
    if not _path_within(full_path, real_library_root):
        abort(403)

    if not os.path.isfile(full_path):
        abort(404)

    return _serve_file(full_path)


@bp.route("/ingest-preview")
@login_required
@require_library()
def stream_ingest_preview():
    """
    Stream a pre-ingest audio file by folder + filename.
    Used during ingest review so archivists can preview tracks
    before they exist in the DB.
    """
    folder   = request.args.get("folder", "").strip()
    filename = request.args.get("file",   "").strip()
    if not folder or not filename:
        abort(400)

    # Resolve the folder and confirm it lives inside one of the configured
    # IMPORT_ROOTS — otherwise any logged-in user could read arbitrary files
    # off the server (e.g. ?folder=/etc&file=passwd).
    real_folder  = os.path.realpath(folder)
    if not within_import_roots(real_folder):
        abort(403)

    # Resolve the file and confirm it stays inside the folder (no traversal)
    full_path = os.path.realpath(os.path.join(real_folder, filename))
    if not _path_within(full_path, real_folder):
        abort(403)
    if not os.path.isfile(full_path):
        abort(404)

    return _serve_file(full_path)


def _parse_range(range_header, file_size):
    """Parse a Range header and return (start, end) byte positions."""
    try:
        units, range_spec = range_header.split("=")
        start_str, end_str = range_spec.split("-")
        byte_start = int(start_str)
        byte_end   = int(end_str) if end_str else file_size - 1
    except (ValueError, AttributeError):
        byte_start = 0
        byte_end   = file_size - 1
    return byte_start, min(byte_end, file_size - 1)
