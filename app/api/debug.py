"""api/debug.py — Debug endpoints. ADMIN ONLY.

Routes:
  POST /api/debug/log   receive a JS log entry (in-memory buffer)
  GET  /api/debug/live  current in-memory log (polled by pop-out AND the
                        in-app panel's Live Server Activity section)
  GET  /api/debug/info  general app state

The in-memory buffer itself lives in app/utils/debug_log.py so server-side
pipelines (scan_folder, read_flac_tags, parse_info_file, Paula scoring) can
push "step" checkpoints into it directly via log_step, not just receive entries
forwarded from the JS fetch wrapper after the fact — see that module's
docstring for why (a stuck Batch Import scan was completely invisible before
this).

(The on-disk FLAC tag viewer moved to the always-on "File Tags" pane:
 GET /api/recordings/<id>/tags.)
"""

from pathlib import Path

from flask import Blueprint, jsonify, request, current_app, abort
from flask_login import login_required, current_user

from app.utils.debug_log import log_entry, all_entries

from app.extensions import db
from app.models.recording import Recording

bp = Blueprint("debug", __name__)


def _require_admin():
    """Abort 404 for anyone who is not an admin.

    That made the debug panel absent from `python3 run.py` — the app actually
    being tested — so it was never there at the moment something broke. Gating
    on the role instead makes it available while testing and still invisible to
    a listener.

    404 rather than 403 deliberately: a debug surface should not confirm its
    own existence to someone who has no business with it. This check is the
    real boundary — hiding the tab in the frontend is only a courtesy.
    """
    if not (current_user.is_authenticated and current_user.role == "admin"):
        abort(404)


# ── POST /api/debug/log — JS forwards entries here ───────────────────────────

@bp.route("/log", methods=["POST"])
@login_required
def debug_log():
    """Receive a log entry from the JS layer and append to the in-memory buffer."""
    _require_admin()
    entry = request.get_json(silent=True) or {}
    log_entry(entry)
    return '', 204


# ── GET /api/debug/live — polled by the pop-out browser window AND the ───────
# in-app panel (while open) for live server-side step checkpoints.

@bp.route("/live")
@login_required
def debug_live():
    """Return the full in-memory log as JSON."""
    _require_admin()
    return jsonify(all_entries())


# ── GET /api/debug/info ───────────────────────────────────────────────────────

@bp.route("/info")
@login_required
def debug_info():
    _require_admin()

    from app.models.artist import Artist
    from app.models.musician import Musician
    from app.models.track import Track

    return jsonify({
        "dev_mode":    current_app.config.get("DEV_MODE"),
        "library_root": str(current_app.config.get("LIBRARY_ROOT", "")),
        "db_path":     str(current_app.config.get("SQLALCHEMY_DATABASE_URI", "")),
        "user":        {"id": current_user.id, "username": current_user.username},
        "counts": {
            "artists": db.session.query(Artist).count(),
            "musicians":    db.session.query(Musician).count(),
            "recordings": db.session.query(Recording).count(),
            "tracks":     db.session.query(Track).count(),
        },
        "app":     _app_info(),
        "schema":  _schema_info(),
        "startup": current_app.config.get("DB_UPGRADE_RESULT"),
        "backups": _backups_info(),
        "update":  _update_info(),
        "paths": {
            "data_dir":     str(current_app.config.get("DATA_DIR")),
            "database":     str(current_app.config.get("DB_PATH")),
            "library_root": str(current_app.config.get("LIBRARY_ROOT")),
        },
    })


def _db_file():
    from app.utils.db_upgrade import sqlite_path_from_uri
    return sqlite_path_from_uri(current_app.config.get("SQLALCHEMY_DATABASE_URI", ""))


def _app_info():
    import sqlite3
    import sys
    import alembic
    from config import is_installed_app
    from version import __version__, APP_NAME
    return {
        "version": __version__,
        "name": APP_NAME,
        "installed": is_installed_app(),
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "alembic": alembic.__version__,
    }


def _schema_info():
    """Stored vs head revision and the revisions between. Read-only."""
    out = {"stored": None, "head": None, "pending": []}
    try:
        from app.utils.db_upgrade import alembic_config, head_and_known, read_state
        path = _db_file()
        if not path:
            out["error"] = "not a file database"
            return out
        cfg = alembic_config(path)
        head, _known = head_and_known(cfg)
        out["head"] = head
        _tables, stored = read_state(path)
        out["stored"] = stored
        if stored != head:
            from alembic.script import ScriptDirectory
            script = ScriptDirectory.from_config(cfg)
            revs = script.iterate_revisions(head, stored) if stored else script.walk_revisions()
            out["pending"] = [r.revision for r in revs if r.revision != stored]
    except Exception as e:
        out["error"] = str(e) or e.__class__.__name__
    return out


def _backups_info():
    from datetime import datetime, timezone
    path = _db_file()
    if not path:
        return []
    folder = Path(path).parent / "backups"
    rows = []
    try:
        for f in folder.glob("trellis-*.db"):
            st = f.stat()
            rows.append({
                "name": f.name, "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            })
    except OSError:
        return []
    rows.sort(key=lambda r: r["modified"], reverse=True)
    return rows


def _update_info():
    try:
        from app.utils import update_check
        return update_check.status()
    except Exception as e:
        return {"error": str(e) or e.__class__.__name__}


