"""
app/utils/install_epoch.py — write-once record of when this install first
ran. Local only, never transmitted. NON-FATAL BY DESIGN, same principle as
folder_naming.rename_recording_folder: runs on every launch, so any failure
is logged and swallowed, never raised.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _install_epoch_path():
    """Beside the database (never a DB row), derived off the RESOLVED
    database path exactly as Config.AVATAR_DIR is — a symlinked install and
    a second node get their own file. Reads DB_PATH off current_app.config,
    not the Config class, so a test app's temp DB is never touched for real
    (same convention app/api/auth.py uses for AVATAR_DIR)."""
    return Path(current_app.config["DB_PATH"]).resolve().parent.parent / "install.json"


def _owner_created_at():
    """The owner (admin-role) User row's created_at, tz-aware, or None — no
    DB/table/row yet is an absent signal, not a failure."""
    try:
        from app.extensions import db
        from app.models.user import User
        owner = (db.session.query(User).filter_by(role="admin")
                 .order_by(User.id.asc()).first())
        if owner is None or owner.created_at is None:
            return None
        created = owner.created_at
        return created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _db_file_birthtime():
    """The DB file's creation time, or None. st_birthtime is macOS/BSD-only;
    falls back to st_ctime elsewhere (Windows is on the roadmap)."""
    try:
        st = os.stat(current_app.config["DB_PATH"])
        ts = getattr(st, "st_birthtime", None)
        return datetime.fromtimestamp(ts if ts is not None else st.st_ctime, tz=timezone.utc)
    except Exception:
        return None


def _earliest_epoch():
    """Earliest credible signal of when this install began, and which rule
    won. Falls back to now only when neither signal exists."""
    candidates = [(name, dt) for name, dt in (
        ("owner_created_at", _owner_created_at()),
        ("db_file_birthtime", _db_file_birthtime()),
    ) if dt is not None]
    if not candidates:
        return "new_install", datetime.now(timezone.utc)
    return min(candidates, key=lambda pair: pair[1])


def ensure_install_epoch():
    """
    Write install.json the first time this install ever runs; never touch
    it again. NOT tied to run.py's "database file is missing" first-run
    path: an install already on v0.1.0-v0.2.3 has a database and an owner
    row that predate this feature, and must be BACKDATED to them rather than
    stamped with the day of the upgrade — so this checks for its OWN file,
    not for a fresh database.

    Called on every launch. Only a real failure to read or write
    install.json is caught and logged (by name, never as something else) —
    the signal lookups above already treat "nothing there yet" as data.
    """
    try:
        path = _install_epoch_path()
        if path.exists():
            return
    except Exception:
        log.exception("install epoch: could not resolve or check install.json path")
        return

    source, epoch_dt = _earliest_epoch()
    payload = {
        "install_id": uuid.uuid4().hex,
        "epoch": epoch_dt.astimezone(timezone.utc).isoformat(),
        "epoch_source": source,
        "schema": SCHEMA_VERSION,
    }
    try:
        from version import __version__
        payload["app_version"] = __version__
    except Exception:
        pass

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        log.exception("install epoch: failed to write %s", path)
