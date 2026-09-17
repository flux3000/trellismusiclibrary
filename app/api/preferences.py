"""
api/preferences.py — read/write user preferences.

Non-secret prefs round-trip through user_preference. The Anthropic API key is
stored in the OS keychain (see utils/prefs); only its presence is exposed.
"""

from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user

from app.utils.prefs import (get_pref, set_pref, has_api_key,
                             set_api_key, delete_api_key, _HAS_KEYRING)

bp = Blueprint("preferences", __name__)

_ALLOWED_MODELS = {"claude-sonnet-5", "claude-haiku-4-5"}


def _snapshot(uid):
    return {
        "ai_model":             get_pref(uid, "ai_model", "claude-sonnet-5"),
        # DEFAULT CHANGED TO "move" 2026-08-07: Ryan started seeing duplicates
        # in the library. Copy leaves the source in the import folder, so a
        # re-scan of that folder offers the same show again and a second
        # ingest is one click away. Move makes re-ingesting the same files
        # structurally impossible, which is the stronger guarantee.
        "ingest_file_behavior": get_pref(uid, "ingest_file_behavior", "move"),
        "has_api_key":          has_api_key(uid),
        "keychain_available":   _HAS_KEYRING,
        # Server-owned, not a user preference — surfaced here so the frontend
        # never hardcodes a filesystem path. Read-only: update_preferences
        # ignores it.
        "import_dir":           current_app.config.get("IMPORT_DIR", ""),
        # Which triage destinations this install actually HAS (2026-09-17).
        # Server-owned like import_dir. A library Trellis laid out itself has
        # both; a library the user imported may have neither, one, or both,
        # because those folders are optional and not ours to invent (Ryan).
        # The frontend hardcoded Backlog and Workshop in three places, so
        # without this an install that has no Backlog still offered the
        # button and the move 400'd on click -- a failure reported as a
        # different failure, which this codebase has a rule about.
        "triage_destinations":  sorted(current_app.config.get("TRIAGE_DIRS", {})),
    }


@bp.route("", methods=["GET"])
@bp.route("/", methods=["GET"])
@login_required
def get_preferences():
    return jsonify(_snapshot(current_user.id))


@bp.route("", methods=["PUT"])
@bp.route("/", methods=["PUT"])
@login_required
def update_preferences():
    uid  = current_user.id
    data = request.get_json() or {}

    if data.get("ai_model") in _ALLOWED_MODELS:
        set_pref(uid, "ai_model", data["ai_model"])
    if data.get("ingest_file_behavior") in ("move", "copy"):
        set_pref(uid, "ingest_file_behavior", data["ingest_file_behavior"])

    # API key: a non-empty string sets it; clear_api_key removes it.
    if data.get("clear_api_key"):
        delete_api_key(uid)
    elif "api_key" in data:
        val = (data.get("api_key") or "").strip()
        if val:
            if not _HAS_KEYRING:
                return jsonify({"error": "OS keychain unavailable on this system"}), 501
            set_api_key(uid, val)

    return jsonify(_snapshot(uid))
