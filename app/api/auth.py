"""
api/auth.py — Authentication endpoints.

Routes:
  POST   /api/auth/login       — validate credentials, create session
  POST   /api/auth/logout      — clear session
  GET    /api/auth/me          — current user, including profile
  PATCH  /api/auth/me          — change the sign-in name or display name
  POST   /api/auth/me/avatar   — set the picture
  DELETE /api/auth/me/avatar   — remove it
  GET    /api/auth/me/avatar   — serve it
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, request, jsonify, current_app, send_file
from flask_login import login_user, logout_user, current_user, login_required
import bcrypt
from app.extensions import db
from app.models.user import User
from app.utils.entity_images import ALLOWED_IMAGE_EXTS

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["POST"])
def login():
    data     = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "")

    user = db.session.query(User).filter_by(username=username, is_active=True).first()
    if not user or not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        return jsonify({"error": "Invalid credentials"}), 401

    login_user(user)
    return jsonify({"id": user.id, "username": user.username, "role": user.role})


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return jsonify({"ok": True})


# ── Profile ───────────────────────────────────────────────────────────────────
# Added 2026-08-25. `username` is the credential and does not change here;
# `display_name` and the picture are what a human sees, and both travel to
# anyone this person shares a library with (see api/share.py:_node_identity).

_AVATAR_MAX_BYTES = 4 * 1024 * 1024        # a face, not a photograph library


def _avatar_dir():
    d = Path(current_app.config["AVATAR_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _avatar_path(user):
    """None when this user has no picture recorded."""
    if not user.avatar_ext:
        return None
    return _avatar_dir() / f"user_{user.id}{user.avatar_ext}"


def profile_payload(user):
    return {
        "id":           user.id,
        "username":     user.username,          # the credential — editable since 2026-08-28
        "display_name": user.display_name,      # may be None
        "name":         user.name,              # what to SHOW; never blank
        "has_avatar":   bool(user.avatar_ext),
        # Cache-busted on the extension so replacing a picture actually repaints.
        "avatar_url":   f"/api/auth/me/avatar?v={user.updated_at.timestamp() if user.updated_at else 0}"
                        if user.avatar_ext else None,
    }


@bp.route("/me", methods=["PATCH"])
@login_required
def update_me():
    """
    Both names, in one request or either alone.

    `username` became editable here on 2026-08-28. It had been fixed for the
    life of the account, which meant the one name a person types on the
    first-run setup page was the one name the app gave them no way to
    correct -- and that page had a bug that could put the wrong name there
    (see FluxAPI._create_owner_account in run.py). Safe to change: the
    username is read by the login lookup, this payload and User.name's
    fallback, and nothing keys off it. Flask-Login carries the row id, so a
    rename does not end the session.

    Every field is VALIDATED before any of them is APPLIED. A patch that
    sets both names must not leave the first one changed when the second is
    rejected.
    """
    data = request.get_json(silent=True) or {}

    new_username     = None      # None = not being changed
    new_display_name = None
    touch_display    = False

    if "username" in data:
        raw = (data.get("username") or "").strip()
        if not raw:
            return jsonify({"error": "A sign-in name can't be empty."}), 400
        if len(raw) > 64:
            return jsonify({"error": "That sign-in name is too long (64 characters max)."}), 400
        if raw != current_user.username:
            taken = (db.session.query(User)
                     .filter(User.username == raw, User.id != current_user.id)
                     .first())
            if taken is not None:
                return jsonify({"error": "Another account here already uses that sign-in name."}), 409
            new_username = raw

    if "display_name" in data:
        raw = (data.get("display_name") or "").strip()
        if len(raw) > 120:
            return jsonify({"error": "That name is too long (120 characters max)."}), 400
        # Empty means "go back to my username", stored as NULL rather than "" so
        # there is one representation of absent.
        new_display_name = raw or None
        touch_display    = True

    if new_username is not None:
        current_user.username = new_username
    if touch_display:
        current_user.display_name = new_display_name

    current_user.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(profile_payload(current_user))


@bp.route("/me/avatar", methods=["POST"])
@login_required
def upload_avatar():
    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify({"error": "No image file provided"}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        return jsonify({
            "error": f"{ext or 'That file'} is not an image Trellis can read. "
                     f"Use {', '.join(sorted(ALLOWED_IMAGE_EXTS))}."
        }), 400

    blob = f.read()
    if len(blob) > _AVATAR_MAX_BYTES:
        return jsonify({"error": "That picture is larger than 4 MB."}), 400
    if not blob:
        return jsonify({"error": "That file is empty."}), 400

    # Remove the old one FIRST when the extension differs, or a .png replaced by
    # a .jpg leaves the .png behind forever and nothing will ever look at it.
    old = _avatar_path(current_user)
    if old and old.exists() and old.suffix != ext:
        try: old.unlink()
        except OSError: pass

    path = _avatar_dir() / f"user_{current_user.id}{ext}"
    path.write_bytes(blob)

    current_user.avatar_ext = ext
    current_user.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(profile_payload(current_user)), 201


@bp.route("/me/avatar", methods=["DELETE"])
@login_required
def delete_avatar():
    """
    The file goes before the column is cleared — the same ordering the library's
    destructive operations use. If the unlink fails the row still points at a
    real file, which is recoverable; clearing first would orphan it forever.
    """
    path = _avatar_path(current_user)
    if path and path.exists():
        path.unlink()
    current_user.avatar_ext = None
    current_user.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(profile_payload(current_user))


@bp.route("/me/avatar")
@login_required
def serve_avatar():
    path = _avatar_path(current_user)
    if not path or not path.exists():
        return jsonify({"error": "No picture"}), 404
    return send_file(str(path), mimetype=ALLOWED_IMAGE_EXTS[current_user.avatar_ext])


@bp.route("/me")
@login_required
def me():
    payload = profile_payload(current_user)
    payload.update({
        "role":        current_user.role,
        "all_artists": current_user.all_artists,
    })
    return jsonify(payload)
