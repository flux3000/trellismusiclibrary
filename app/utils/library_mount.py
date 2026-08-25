"""
utils/library_mount.py — is the library drive actually there?

LIBRARY_ROOT lives on an SMB share (/Volumes/music). When macOS drops that
mount the database is still perfectly fine — every Recording, Track, Performer
and Venue row is local SQLite — but every byte of audio and every image is
suddenly gone. Without this module the app finds that out one broken <img> at
a time.

The contract: `status()` is cheap, never raises, and never blocks the request
thread for long. Callers get a dict they can hand straight to the frontend.

Two things make this less trivial than os.path.isdir():

  1. A half-dead SMB mount hangs I/O indefinitely. A bare listdir() on a
     disconnected share can park a Flask worker forever, so the probe runs in a
     daemon thread with a hard timeout and we accept a leaked thread over a
     wedged request.

  2. `mount` succeeding is not the same as the app working. If a stale
     directory squats on /Volumes/music, macOS mounts the share at
     /Volumes/music-1 instead and LIBRARY_ROOT resolves to a real, empty,
     local directory. isdir() says True and nothing works. So we check
     ismount() on the volume root as well, and report that case distinctly —
     it is the single most confusing failure this project has had.

See tools/mount_library.py for the LaunchAgent that repairs what this detects.
"""

import os
import threading
import time

from flask import current_app

# Cache. The banner polls every 30s and pre-flight guards fire on every
# drive-dependent request; without a TTL a page with 40 images would stat the
# share 40 times.
_TTL           = 5.0
_lock          = threading.Lock()
_cached        = None    # last computed status dict
_cached_at     = 0.0

_PROBE_TIMEOUT = 3.0     # seconds before we call the mount hung


# ── Reason codes (also consumed by the frontend) ─────────────────────────────
#   ok               connected and readable
#   volume_missing   the volume root is not present at all — drive not mounted
#   not_mounted      volume root exists but is a plain directory, not a mount.
#                    Almost always a squatter dir; the share is probably at
#                    <name>-1. See tools/mount_library.py teardown step 2.
#   unreadable       mounted, but LIBRARY_ROOT cannot be listed (permissions,
#                    or the Flux Audio folder moved on the NAS)
#   timeout          I/O hung — treat as disconnected, the mount is half-dead


def _volume_root(path):
    """
    The mountpoint a path lives under, for macOS /Volumes paths.

    Returns e.g. '/Volumes/music' for '/Volumes/music/Trellis/Library'.
    None when the library is on the boot volume (a dev machine pointing
    LIBRARY_ROOT at a local folder), in which case there is no mount to check.
    """
    parts = os.path.normpath(str(path)).split(os.sep)
    # ['', 'Volumes', 'music', 'Trellis', 'Library']
    if len(parts) >= 3 and parts[1] == "Volumes":
        return os.sep.join(parts[:3])
    return None


def _probe(library_root):
    """The actual filesystem checks. Runs on a throwaway thread."""
    vol = _volume_root(library_root)

    if vol is not None:
        if not os.path.exists(vol):
            return "volume_missing", vol
        if not os.path.ismount(vol):
            # A real directory sitting where a mount should be.
            return "not_mounted", vol

    if not os.path.isdir(library_root):
        return "unreadable", vol
    try:
        os.listdir(library_root)
    except OSError:
        return "unreadable", vol

    return "ok", vol


def _compute(library_root):
    """Run _probe with a hard timeout so a dead mount cannot wedge a worker."""
    result = {}

    def target():
        try:
            result["value"] = _probe(library_root)
        except Exception:                      # noqa: BLE001 — never propagate
            result["value"] = ("unreadable", None)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(_PROBE_TIMEOUT)

    if "value" not in result:
        # Thread is still blocked on I/O. It is a daemon, so it will not hold
        # the process open; we simply stop waiting on it.
        return "timeout", _volume_root(library_root)
    return result["value"]


def status(force=False):
    """
    Current library-drive status. Cheap, cached, never raises.

    {
      "connected":    bool,
      "reason":       one of the codes above,
      "volume":       '/Volumes/music' or None,
      "library_root": str,
      "checked_at":   epoch seconds,
    }
    """
    global _cached, _cached_at

    library_root = str(current_app.config.get("LIBRARY_ROOT", ""))
    now = time.time()

    # Serve from cache unless it is stale or the caller insists.
    if not force and _cached is not None and (now - _cached_at) < _TTL:
        if _cached.get("library_root") == library_root:
            return _cached

    # Single-flight: if another thread is mid-probe, hand back what we have
    # rather than queueing up behind it. A stale-by-seconds answer is fine;
    # a pile-up of blocked workers is not.
    if not _lock.acquire(blocking=False):
        return _cached or {
            "connected": True, "reason": "ok", "volume": None,
            "library_root": library_root, "checked_at": now,
        }

    try:
        reason, volume = _compute(library_root)
        _cached = {
            "connected":    reason == "ok",
            "reason":       reason,
            "volume":       volume,
            "library_root": library_root,
            "checked_at":   now,
        }
        _cached_at = now
        return _cached
    finally:
        _lock.release()


def is_connected():
    """Boolean shorthand for guards and templates."""
    return status()["connected"]


def invalidate():
    """
    Drop the cache so the next status() re-probes immediately.

    Call after anything that plausibly changed the mount — a manual reconnect,
    or an operation that just failed with ENOENT under LIBRARY_ROOT.
    """
    global _cached_at
    _cached_at = 0.0
