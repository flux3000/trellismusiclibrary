"""
api/archive.py -- browse an archive's catalog (2026-10-01).

Routes (all admin-only; this surface exists to put files on the owner's disk):
    GET /api/archive/lma/recent?page=&sort=newest|date|az
    GET /api/archive/lma/search?q=&page=&sort=
    GET /api/archive/lma/item/<id>

The connector returns archive facts; this layer adds the two fields that depend
on THIS install: `in_library` (artist + date match against Recordings) and
`job` (a queued/active download of the same item). Never registered in
SERVER_MODE (see app/__init__.py).
"""

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from app.extensions import db
from app.models.artist import Artist
from app.models.download_job import DownloadJob
from app.models.performance import Performance
from app.models.recording import Recording
from app.sources import get_source, SourceError, SourceNotFound
from app.utils.authz import admin_required

bp = Blueprint("archive", __name__)

_SORTS = ("newest", "date", "az")


def _parse_date(s):
    """(y, m, d) for a full YYYY-MM-DD, else None. A partial date never matches:
    'artist + date' means the show, and a bare year would claim every show."""
    try:
        y, m, d = (int(x) for x in (s or "").split("-"))
        return y, m, d
    except ValueError:
        return None


def in_library_map(items):
    """
    {(artist_lower, y, m, d): [{recording_id, source}]} for every item on the
    page, in ONE query. Two recordings of one show (a common case) both come
    back; matching never blocks anything, it only informs.
    """
    keys = {}
    for it in items:
        d = _parse_date(it.get("date"))
        if d and it.get("artist"):
            keys[(it["artist"].strip().lower(),) + d] = None
    if not keys:
        return {}
    names = {k[0] for k in keys}
    years = {k[1] for k in keys}
    rows = (db.session.query(func.lower(Artist.name), Performance.start_year,
                             Performance.start_month, Performance.start_day,
                             Recording.id, Recording.source)
            .join(Performance, Performance.artist_id == Artist.id)
            .join(Recording, Recording.performance_id == Performance.id)
            .filter(func.lower(Artist.name).in_(names),
                    Performance.start_year.in_(years))
            .order_by(Recording.id)
            .all())
    out = {}
    for name, y, m, d, rid, src in rows:
        k = (name, y, m, d)
        if k in keys:
            out.setdefault(k, []).append({"recording_id": rid, "source": src})
    return out


def _open_jobs(items):
    ids = [it["id"] for it in items]
    if not ids:
        return {}
    rows = (db.session.query(DownloadJob)
            .filter(DownloadJob.source_id.in_(ids),
                    DownloadJob.status.in_(("queued", "active")))
            .order_by(DownloadJob.id).all())
    return {(j.source, j.source_id): {"id": j.id, "status": j.status} for j in rows}


def annotate(items):
    lib = in_library_map(items)
    jobs = _open_jobs(items)
    for it in items:
        d = _parse_date(it.get("date"))
        key = ((it.get("artist") or "").strip().lower(),) + d if d else None
        it["in_library"] = lib.get(key, []) if key else []
        it["job"] = jobs.get((it["source"], it["id"]))
    return items


def _page_args():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        return None, None
    sort = request.args.get("sort", "newest")
    if sort not in _SORTS:
        return None, None
    return page, sort


def _source():
    return get_source("lma")


@bp.route("/lma/recent", methods=["GET"])
@admin_required
def lma_recent():
    page, sort = _page_args()
    if page is None:
        return jsonify({"error": "Bad page or sort"}), 400
    try:
        items, more = _source().recent(page, sort)
    except SourceError:
        return jsonify({"error": "The Archive could not be reached"}), 502
    items = annotate([dict(i) for i in items])
    return jsonify({"items": items, "page": page, "has_more": more})


@bp.route("/lma/search", methods=["GET"])
@admin_required
def lma_search():
    page, sort = _page_args()
    if page is None:
        return jsonify({"error": "Bad page or sort"}), 400
    q = (request.args.get("q") or "").strip()
    try:
        src = _source()
        items, more = src.search(q, page, sort) if q else src.recent(page, sort)
    except SourceError:
        return jsonify({"error": "The Archive could not be reached"}), 502
    items = annotate([dict(i) for i in items])
    return jsonify({"items": items, "page": page, "has_more": more})


@bp.route("/lma/item/<item_id>", methods=["GET"])
@admin_required
def lma_item(item_id):
    try:
        item = dict(_source().item(item_id))
    except SourceNotFound:
        return jsonify({"error": "Not found"}), 404
    except SourceError:
        return jsonify({"error": "The Archive could not be reached"}), 502
    annotate([item])
    return jsonify(item)
