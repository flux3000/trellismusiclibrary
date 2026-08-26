"""
api/system.py — app-level health the frontend needs in order to behave.

Right now that is one thing: is the library drive connected?

Routes:
  GET  /api/system/library-status    polled by the Action Bar banner (~30s)
  POST /api/system/library-recheck   drop the cache and probe immediately

Note what is deliberately NOT here: an endpoint that mounts the drive. Mount
ownership belongs to the LaunchAgent (tools/mount_library.py), which runs
whether or not Flux is open and keeps the share available to Finder and
everything else. The app's job is to notice and report, not to mount.

This module also exposes `require_library`, the pre-flight guard that
drive-dependent endpoints wear so they fail cleanly instead of half-working.
"""

from functools import wraps

from flask import Blueprint, jsonify, Response, current_app
from config import is_installed_app
from flask_login import login_required

from app.utils import library_mount

bp = Blueprint("system", __name__)


# ── Human-readable copy for each reason code ─────────────────────────────────
# Kept server-side so the message and the diagnosis never drift apart.

_MESSAGES = {
    "ok":             "Library drive connected.",
    "volume_missing": "The library drive is not mounted. Audio and images are "
                      "unavailable until it reconnects.",
    "not_mounted":    "A folder is sitting where the library drive should be "
                      "mounted — the share has probably mounted under a "
                      "different name. Audio and images are unavailable.",
    "unreadable":     "The library drive is mounted but its contents cannot be "
                      "read. Check the share on the NAS.",
    "timeout":        "The library drive is not responding. It may have gone to "
                      "sleep or dropped off the network.",
}


def _payload():
    st = library_mount.status()
    return {**st, "message": _MESSAGES.get(st["reason"], _MESSAGES["unreadable"])}


@bp.route("/library-status")
@login_required
def library_status():
    """Cheap, cached, safe to poll."""
    return jsonify(_payload())


@bp.route("/library-recheck", methods=["POST"])
@login_required
def library_recheck():
    """Force a fresh probe — for the banner's 'Check again' action."""
    library_mount.invalidate()
    return jsonify(_payload())


# ── Pre-flight guard ─────────────────────────────────────────────────────────

# A 1x1-ish neutral placeholder. Served in place of artwork when the drive is
# gone so the UI shows a calm empty frame rather than a broken-image icon.
# no-store matters: the moment the drive returns, the next render must refetch
# the real image instead of a cached placeholder.
_PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
    'role="img" aria-label="Image unavailable — library drive disconnected">'
    '<rect width="100" height="100" fill="#1c1c1e"/>'
    '<path d="M32 62l14-18 10 12 7-8 9 14z" fill="#3a3a3d"/>'
    '<circle cx="63" cy="37" r="5" fill="#3a3a3d"/>'
    '</svg>'
)


@bp.route("/about")
@login_required
def about():
    """
    What this install IS. Answers the first question of every support
    conversation — which version, and where is the data — without asking anyone
    to find a terminal.

    Paths are deliberately shown: a packaged app hides its database somewhere a
    person would never guess, and "where did my library go" is a fair question
    to be able to answer from inside the app.
    """
    from version import __version__, APP_NAME
    return jsonify({
        "app_name":     APP_NAME,
        "version":      __version__,
        "installed":    is_installed_app(),
        "data_dir":     str(current_app.config.get("DATA_DIR")),
        "database":     str(current_app.config.get("DB_PATH")),
        "library_root": str(current_app.config.get("LIBRARY_ROOT")),
    })


def require_library(kind="json"):
    """
    Refuse to run an endpoint when the library drive is disconnected.

    kind="json"   → 503 with a typed error body. The frontend keys off
                    code == "library_disconnected" to raise the banner
                    immediately rather than waiting for the next poll.
    kind="image"  → a placeholder SVG instead of a broken image. This is the
                    last line of defence; the frontend should already be
                    rendering placeholders, but a request that slips through
                    during the poll window must not produce a red X.

    Guards are cheap — library_mount.status() is cached for 5s — so wearing
    one costs a dict lookup on the hot path.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            st = library_mount.status()
            if st["connected"]:
                return fn(*args, **kwargs)

            if kind == "image":
                return Response(
                    _PLACEHOLDER_SVG,
                    mimetype="image/svg+xml",
                    headers={"Cache-Control": "no-store"},
                )

            return jsonify({
                "error":   _MESSAGES.get(st["reason"], _MESSAGES["unreadable"]),
                "code":    "library_disconnected",
                "reason":  st["reason"],
            }), 503
        return wrapper
    return decorator
