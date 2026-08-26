"""
app/utils/transcode.py — on-demand transcode + disposable cache.

Peers never receive raw FLAC. First play of a track transcodes it to MP3 256k
via ffmpeg and writes the result to a cache directory (default: <repo>/cache/
transcodes, overridable via TRANSCODE_CACHE_DIR); every subsequent play serves
straight from cache. The cache is disposable — deleting it just forces a
re-transcode on next play. It is deliberately NOT inside the library folder,
so the pristine originals are never touched. See Design Spec v1.

Notes:
- Source FLAC is read from the library (on the Mac Mini that means over the LAN
  from the NAS mount); the transcode is written locally on the serving box.
- A per-cache-key lock prevents two concurrent first-plays from transcoding the
  same track twice, and the write-to-temp-then-atomic-rename means a reader can
  never catch a half-written file.
"""

import os
import shutil
import subprocess
import threading

from flask import current_app

DEFAULT_FORMAT  = "mp3"
DEFAULT_BITRATE = "256k"
_MIMETYPES = {"mp3": "audio/mpeg"}

# One lock per cache key, created on demand. Guarded by _locks_guard.
_locks = {}
_locks_guard = threading.Lock()


class FfmpegMissing(RuntimeError):
    """ffmpeg could not be found."""


# Where Homebrew puts things, plus the system location. Apple Silicon uses the
# first, Intel Macs the second.
_FFMPEG_FALLBACKS = (
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
)


def resolve_ffmpeg():
    """
    Find ffmpeg, without assuming a login shell's PATH.

    This matters specifically because of packaging (2026-08-25). An app launched
    by double-clicking does NOT inherit the PATH from your terminal — macOS gives
    it a minimal one, typically /usr/bin:/bin:/usr/sbin:/sbin. Homebrew installs
    to /opt/homebrew/bin, which is not on that list. So `ffmpeg` resolves
    perfectly from a terminal and not at all from the Dock, and the symptom is
    every track failing to play with "Transcoder unavailable" — which reads like
    a network or sharing fault, not a missing program.

    Order: an explicit setting wins (including a wrong one, so a typo fails
    loudly rather than being silently corrected), then PATH, then the usual
    install locations.
    """
    configured = current_app.config.get("FFMPEG_BIN")
    if configured:
        return configured

    found = shutil.which("ffmpeg")
    if found:
        return found

    for candidate in _FFMPEG_FALLBACKS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return "ffmpeg"      # let the run fail, and say where we looked


class SourceMissing(RuntimeError):
    """The source audio file could not be found on disk."""


def mimetype_for(fmt=DEFAULT_FORMAT):
    return _MIMETYPES.get(fmt, "application/octet-stream")


def _cache_dir():
    configured = current_app.config.get("TRANSCODE_CACHE_DIR")
    if configured:
        path = configured
    else:
        # <repo root>/cache/transcodes  (app.root_path is the app/ package dir)
        path = os.path.join(current_app.root_path, "..", "cache", "transcodes")
    path = os.path.abspath(path)
    os.makedirs(path, exist_ok=True)
    return path


def _lock_for(key):
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def get_or_create_transcode(track, fmt=DEFAULT_FORMAT, bitrate=DEFAULT_BITRATE):
    """Return the filesystem path to a cached transcode of `track`, creating it
    with ffmpeg if it doesn't exist yet. Raises SourceMissing / FfmpegMissing."""
    from app.models.recording import Recording
    from app.extensions import db

    recording = db.session.get(Recording, track.recording_id)
    if recording is None:
        raise SourceMissing("recording not found")

    library_root = current_app.config["LIBRARY_ROOT"]
    src = os.path.join(library_root, recording.folder_path, track.file_path)
    if not os.path.isfile(src):
        raise SourceMissing(src)

    key       = f"{track.id}_{fmt}_{bitrate}"
    cache_dir = _cache_dir()
    dest      = os.path.join(cache_dir, f"{key}.{fmt}")

    # Fast path — already cached and non-empty.
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return dest

    lock = _lock_for(key)
    with lock:
        # Re-check inside the lock: another thread may have just built it.
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return dest

        tmp = dest + ".part"
        ffmpeg = resolve_ffmpeg()
        cmd = [
            ffmpeg, "-nostdin", "-y",
            "-i", src,
            "-map", "0:a:0",          # first audio stream only
            "-codec:a", "libmp3lame",
            "-b:a", bitrate,
            "-f", fmt,
            tmp,
        ]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise FfmpegMissing(
                f"ffmpeg not found (tried {ffmpeg!r}, PATH, and "
                f"{', '.join(_FFMPEG_FALLBACKS)}). Install it, or set "
                f"FFMPEG_BIN to its full path."
            )

        if proc.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            # Clean up a failed/partial temp file, surface the error.
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            err = (proc.stderr or b"").decode("utf-8", "replace")[-500:]
            raise RuntimeError(f"ffmpeg failed (rc={proc.returncode}): {err}")

        os.replace(tmp, dest)   # atomic — readers see either old-absent or full file
        return dest
