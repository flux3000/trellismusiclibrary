"""
api/downloads.py -- the download queue and the Downloads folder (2026-10-01).

Routes (admin-only; never registered in SERVER_MODE):
    GET    /api/downloads/queue
    POST   /api/downloads/queue {source, id}
    POST   /api/downloads/queue/<id>/cancel | /retry
    DELETE /api/downloads/queue/<id>
    POST   /api/downloads/queue/reorder {order: [ids]}
    POST   /api/downloads/queue/pause | /resume
    GET    /api/downloads/folder

Move reuses /api/quality/move; Ingest reuses the Add Recordings analysis flow.
"""

import json
import os
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request

from app.extensions import db
from app.models.download_job import DownloadJob
from app.sources import get_source, SourceError
from app.utils import download_queue as dq
from app.utils.authz import admin_required
from app.utils.downloads_dir import ensure_downloads_dir, downloads_dir, DownloadsDirError

bp = Blueprint("downloads", __name__)

_FINISHED_SHOWN = 50
_AUDIO_FORMATS = {".flac": "FLAC", ".shn": "SHN", ".mp3": "MP3", ".wav": "WAV",
                  ".aif": "AIFF", ".aiff": "AIFF", ".m4a": "M4A", ".ogg": "OGG"}


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def job_dict(j):
    return {"id": j.id, "source": j.source, "source_id": j.source_id,
            "artist": j.artist, "date": j.date, "venue": j.venue,
            "status": j.status, "position": j.position,
            "total_bytes": j.total_bytes or 0, "done_bytes": j.done_bytes or 0,
            "folder": os.path.basename((j.dest_path or "").rstrip(os.sep)),
            "error": j.error, "created_at": _iso(j.created_at),
            "finished_at": _iso(j.finished_at)}


def _job_or_404(job_id):
    return db.session.get(DownloadJob, job_id)


@bp.route("/queue", methods=["GET"])
@admin_required
def get_queue():
    live = (db.session.query(DownloadJob)
            .filter(DownloadJob.status.in_(("active", "queued")))
            .order_by(DownloadJob.status, DownloadJob.position, DownloadJob.id).all())
    # active first, then queued by position ("active" < "queued" alphabetically)
    finished = (db.session.query(DownloadJob)
                .filter(DownloadJob.status.in_(("done", "failed")))
                .order_by(DownloadJob.finished_at.desc(), DownloadJob.id.desc())
                .limit(_FINISHED_SHOWN).all())
    return jsonify({"jobs": [job_dict(j) for j in live + finished],
                    "paused": dq.paused()})


@bp.route("/queue", methods=["POST"])
@admin_required
def post_queue():
    body = request.get_json(silent=True) or {}
    source = str(body.get("source") or "")
    item_id = str(body.get("id") or "")
    src = get_source(source)
    if src is None:
        return jsonify({"error": "Unknown source"}), 400
    if not dq.valid_identifier(item_id):
        return jsonify({"error": "Bad item id"}), 400
    if dq.find_open_job(source, item_id):
        return jsonify({"error": "Already queued"}), 409
    try:
        item = src.item(item_id)
        plan = src.download_plan(item_id)
    except SourceError:
        return jsonify({"error": "The Archive could not be reached"}), 502
    if item.get("stream_only"):
        return jsonify({"error": "Stream only"}), 400
    if not plan:
        return jsonify({"error": "Nothing to download"}), 400
    try:
        job = dq.enqueue(source, item_id, item.get("artist"), item.get("date"),
                         item.get("venue"), sum(f.get("size") or 0 for f in plan))
    except DownloadsDirError:
        return jsonify({"error": "Could not create the Downloads folder"}), 500
    return jsonify(job_dict(job)), 201


@bp.route("/queue/<int:job_id>/cancel", methods=["POST"])
@admin_required
def cancel_job(job_id):
    job = _job_or_404(job_id)
    if job is None:
        return jsonify({"error": "Not found"}), 404
    if not dq.cancel(job):
        return jsonify({"error": "Cannot cancel this download"}), 409
    return jsonify(job_dict(job))


@bp.route("/queue/<int:job_id>/retry", methods=["POST"])
@admin_required
def retry_job(job_id):
    job = _job_or_404(job_id)
    if job is None:
        return jsonify({"error": "Not found"}), 404
    if dq.find_open_job(job.source, job.source_id):
        return jsonify({"error": "Already queued"}), 409
    if not dq.retry(job):
        return jsonify({"error": "Cannot retry this download"}), 409
    return jsonify(job_dict(job))


@bp.route("/queue/<int:job_id>", methods=["DELETE"])
@admin_required
def delete_job(job_id):
    job = _job_or_404(job_id)
    if job is None:
        return jsonify({"error": "Not found"}), 404
    if not dq.remove(job):
        return jsonify({"error": "Cancel it first"}), 409
    return jsonify({"ok": True})


@bp.route("/queue/reorder", methods=["POST"])
@admin_required
def reorder_queue():
    body = request.get_json(silent=True) or {}
    order = body.get("order")
    if (not isinstance(order, list)
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in order)):
        return jsonify({"error": "Bad order"}), 400
    dq.reorder(order)
    return jsonify({"ok": True})


@bp.route("/queue/pause", methods=["POST"])
@admin_required
def pause_queue():
    dq.set_paused(True)
    return jsonify({"paused": True})


@bp.route("/queue/resume", methods=["POST"])
@admin_required
def resume_queue():
    dq.set_paused(False)
    return jsonify({"paused": False})


# -- the folder ----------------------------------------------------------------

def _describe(path):
    files = 0
    size = 0
    newest = os.path.getmtime(path)
    counts = {}
    for root, _dirs, names in os.walk(path):
        for n in names:
            fp = os.path.join(root, n)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            size += st.st_size
            newest = max(newest, st.st_mtime)
            if n.endswith(".part"):
                continue
            files += 1
            fmt = _AUDIO_FORMATS.get(os.path.splitext(n)[1].lower())
            if fmt:
                counts[fmt] = counts.get(fmt, 0) + 1
    fmt = max(counts, key=counts.get) if counts else None
    return files, size, fmt, newest


def _checksum_state(job):
    """("verified" | "failed" | None, [mismatched names]) from a finished job.
    None when no download made the folder, or when nothing in it could be
    verified (the archive gave no MD5 for any file)."""
    if job is None or not job.checksums:
        return None, []
    try:
        info = json.loads(job.checksums)
    except ValueError:
        return None, []
    bad = list(info.get("mismatched") or [])
    if bad:
        return "failed", bad
    return ("verified" if info.get("verified") else None), []


@bp.route("/folder", methods=["GET"])
@admin_required
def get_folder():
    # ?which=backlog|workshop lists a working folder with the same row shape
    # (Ryan, 2026-10-01: Backlog and Workshop get the Downloads page design).
    # Those are never created here; an unset one is a 404.
    which = request.args.get("which") or "downloads"
    triage = current_app.config.get("TRIAGE_DIRS") or {}
    if which in ("backlog", "workshop"):
        path = triage.get(which)
        if not path:
            return jsonify({"error": "Not set"}), 404
        folders = []
        if os.path.isdir(path):
            for name in sorted(os.listdir(path), key=str.lower):
                full = os.path.join(path, name)
                if name.startswith(".") or os.path.islink(full) or not os.path.isdir(full):
                    continue
                try:
                    files, size, fmt, mtime = _describe(full)
                except OSError:
                    continue
                folders.append({
                    "checksums": None, "checksum_errors": [],
                    "name": name, "files": files, "size_bytes": size, "format": fmt,
                    "modified": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                    "downloading": False, "job_id": None})
        return jsonify({"path": path, "folders": folders,
                        "destinations": [k for k in ("workshop", "backlog")
                                         if k != which and triage.get(k)]})
    if which != "downloads":
        return jsonify({"error": "Unknown folder"}), 400
    try:
        path = ensure_downloads_dir()
    except DownloadsDirError:
        path = downloads_dir()
    open_jobs = {os.path.basename(j.dest_path.rstrip(os.sep)): j.id
                 for j in db.session.query(DownloadJob)
                 .filter(DownloadJob.status.in_(("queued", "active"))).all()}
    # Latest finished download per folder name, for the checksum icon.
    latest_done = {}
    for j in (db.session.query(DownloadJob).filter(DownloadJob.status == "done")
              .order_by(DownloadJob.finished_at.desc(), DownloadJob.id.desc()).all()):
        latest_done.setdefault(os.path.basename(j.dest_path.rstrip(os.sep)), j)
    folders = []
    if os.path.isdir(path):
        for name in sorted(os.listdir(path), key=str.lower):
            full = os.path.join(path, name)
            if name.startswith(".") or os.path.islink(full) or not os.path.isdir(full):
                continue
            try:
                files, size, fmt, mtime = _describe(full)
            except OSError:
                continue
            status, bad = _checksum_state(latest_done.get(name))
            folders.append({
                "checksums": status, "checksum_errors": bad,
                "name": name, "files": files, "size_bytes": size, "format": fmt,
                "modified": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                "downloading": name in open_jobs,
                "job_id": open_jobs.get(name)})
    return jsonify({"path": path, "folders": folders,
                    "destinations": [k for k in ("backlog", "workshop") if triage.get(k)]})
