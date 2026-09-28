"""
api/bulk_ingest.py -- Bulk Ingest control surface (spec chunk 5d).

Routes:
    POST /api/bulk-ingest/start           start (or return) the active run
    GET  /api/bulk-ingest/current         status/counts/duplicates/progress
                                          for it -- also `now` (in-progress
                                          folder), `upcoming` (next 8 pending
                                          folders) and `recent` (last 12
                                          ingested, for the progress view's
                                          Now/Up next/Recently added rows)
    POST /api/bulk-ingest/<id>/pause      pause a running run
    POST /api/bulk-ingest/<id>/resume     resume a paused run
    GET  /api/bulk-ingest/<id>/items      paged item list, filterable by
                                          status; status=all is every item
                                          including pending/in_progress, id
                                          ascending (discovery order), for the
                                          progress table; status=done is a
                                          pseudo-filter (not pending/
                                          in_progress), newest first, for the
                                          old log's infinite scroll

Admin-gated throughout EXCEPT GET /current (admin_required, app/utils/
authz.py) -- this is a whole-library, filesystem-touching control surface,
the same tier as peer management and library layout, not ordinary library
editing. GET /current is read-only (N3): it mutates nothing, so any logged-
in user can see a run's status -- otherwise a non-admin viewer never gets
the bulk_ingest nav item or the review split during a run, and their home
routing silently differs from an admin's. Never registered in SERVER_MODE
(see app/__init__.py) -- a share node has no bulk_ingest surface at all.
"""

import json

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from sqlalchemy import func

from app.extensions import db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.quality import RecordingQuality
from app.models.recording import Recording
from app.models.performance import Performance
from app.models.artist import Artist
from app.utils.authz import admin_required
from app.utils.format import format_partial_date
from app.utils import bulk_ingest_run

# Not pending or in_progress -- the pseudo-status the log's infinite scroll
# filters on (GET .../items?status=done). A finished item, whichever of the
# four terminal statuses it landed on. Reason -> result-phrase mapping lives
# client-side (app.js), same phrases the done-summary's reason breakdown
# already uses.
_DONE_STATUSES = ("ingested", "review", "skipped", "failed")

bp = Blueprint("bulk_ingest", __name__)

_ITEM_STATUSES = ("pending", "in_progress", "ingested", "review", "skipped", "failed")


def _serialize_run(run):
    counts = dict.fromkeys(_ITEM_STATUSES, 0)
    rows = (db.session.query(BulkIngestItem.status, func.count(BulkIngestItem.id))
            .filter(BulkIngestItem.run_id == run.id)
            .group_by(BulkIngestItem.status)
            .all())
    for status, n in rows:
        counts[status] = n

    # Reason breakdowns for the redesigned bulk_ingest page (2026-09-27): why an
    # item needs a look, and why one was skipped. Grouped queries, not a
    # per-item loop -- run.items can be tens of thousands of rows on a real
    # collection.
    reasons = {
        reason: n for reason, n in (
            db.session.query(BulkIngestItem.reason, func.count(BulkIngestItem.id))
            .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "review")
            .group_by(BulkIngestItem.reason)
            .all())
        if reason
    }
    skipped_reasons = {
        reason: n for reason, n in (
            db.session.query(BulkIngestItem.reason, func.count(BulkIngestItem.id))
            .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "skipped")
            .group_by(BulkIngestItem.reason)
            .all())
        if reason
    }

    # Studio recordings among the ingested ones, for the "Studio: <N>" aside
    # on the Added line.
    studio = (db.session.query(func.count(BulkIngestItem.id))
              .filter(BulkIngestItem.run_id == run.id,
                      BulkIngestItem.status == "ingested",
                      BulkIngestItem.kind == "studio")
              .scalar()) or 0

    # Listening Quality scoring is a follow-up-queue pass that runs after
    # bulk_ingest (app/api/ingest.py::_handle_score) -- "scorable" is every
    # LIVE recording this run ingested (studio recordings are never scored,
    # 2026-09-27), "scored" is how many of those already carry a
    # RecordingQuality row with an actual number in it (a row can exist with
    # listening_quality still NULL if extraction failed, so the NULL check
    # matters, not just row existence).
    scorable = (db.session.query(func.count(BulkIngestItem.id))
                .filter(BulkIngestItem.run_id == run.id,
                        BulkIngestItem.status == "ingested",
                        BulkIngestItem.kind == "live")
                .scalar()) or 0
    scored = (db.session.query(func.count(BulkIngestItem.id))
              .join(Recording, Recording.id == BulkIngestItem.recording_id)
              .join(RecordingQuality, RecordingQuality.recording_id == Recording.id)
              .filter(BulkIngestItem.run_id == run.id,
                      BulkIngestItem.status == "ingested",
                      BulkIngestItem.kind == "live",
                      RecordingQuality.listening_quality.isnot(None))
              .scalar()) or 0

    return {
        "id":              run.id,
        "root":            run.root,
        "status":          run.status,
        "started_at":      run.started_at.isoformat() if run.started_at else None,
        "finished_at":     run.finished_at.isoformat() if run.finished_at else None,
        "last_error":      run.last_error,
        "counts":          counts,
        "reasons":         reasons,
        "skipped_reasons": skipped_reasons,
        "failed":          counts["failed"],
        "studio":          studio,
        "scored":          scored,
        "scorable":        scorable,
    }


def _current_run():
    """The active run if there is one, else the most recently started run of
    any status -- so /current still has something to report right after a
    run finishes."""
    return (db.session.query(BulkIngestRun)
            .order_by(BulkIngestRun.id.desc())
            .first())


@bp.route("/start", methods=["POST"])
@admin_required
def start():
    from flask import current_app
    library_root = str(current_app.config["LIBRARY_ROOT"])
    run = bulk_ingest_run.start_run(library_root)
    return jsonify(_serialize_run(run))


@bp.route("/current", methods=["GET"])
@login_required
def current():
    run = _current_run()
    if run is None:
        return jsonify({"run": None})

    payload = _serialize_run(run)
    if getattr(current_user, "role", None) != "admin":
        # R2-N1: last_error is a full Python traceback with absolute
        # filesystem paths -- fine for the admin who can already see and
        # touch every path in it, not for a viewer/listener/member who just
        # needs to know a run is in flight.
        payload.pop("last_error", None)

    current_item = (db.session.query(BulkIngestItem)
                    .filter(BulkIngestItem.run_id == run.id,
                            BulkIngestItem.status == "in_progress")
                    .order_by(BulkIngestItem.id.asc())
                    .first())
    payload["current"] = current_item.rel_path.rsplit("/", 1)[-1] if current_item else None
    payload["now"] = payload["current"]

    # "Up next" (spec chunk 5e, 2026-09-27): the next eight pending items in
    # the order process() will actually reach them (id asc), basenames only.
    upcoming_rows = (db.session.query(BulkIngestItem.rel_path)
                     .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "pending")
                     .order_by(BulkIngestItem.id.asc())
                     .limit(8)
                     .all())
    payload["upcoming"] = [rp.rsplit("/", 1)[-1] for (rp,) in upcoming_rows]

    # "Recently added" (spec chunk 5e): the last twelve ingested items,
    # newest first, one join -- never a per-item loop.
    recent_rows = (db.session.query(BulkIngestItem.recording_id, Recording.title,
                                    Recording.kind, Artist.name,
                                    Performance.start_year, Performance.start_month,
                                    Performance.start_day)
                   .join(Recording, Recording.id == BulkIngestItem.recording_id)
                   .join(Performance, Performance.id == Recording.performance_id)
                   .join(Artist, Artist.id == Performance.artist_id)
                   .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "ingested")
                   .order_by(BulkIngestItem.id.desc())
                   .limit(12)
                   .all())
    payload["recent"] = [{
        "recording_id": recording_id,
        "artist":       artist_name,
        "date_text":    format_partial_date(start_year, start_month, start_day),
        "title":        title,
        "kind":         kind,
    } for recording_id, title, kind, artist_name, start_year, start_month, start_day in recent_rows]

    duplicates = []
    if run.status == "done":
        dupe_items = (db.session.query(BulkIngestItem)
                      .filter(BulkIngestItem.run_id == run.id,
                              BulkIngestItem.duplicate_of.isnot(None))
                      .all())
        # N2: the second link used to show the bare recording id as its
        # link text, which reads as a bug -- one join for the duplicate
        # recording's own folder basename instead.
        dup_ids = {it.duplicate_of for it in dupe_items}
        basenames = {}
        if dup_ids:
            basenames = {
                rid: (fp.rsplit("/", 1)[-1] if fp else None)
                for rid, fp in (db.session.query(Recording.id, Recording.folder_path)
                               .filter(Recording.id.in_(dup_ids)).all())
            }
        duplicates = [{
            "item_id":       it.id,
            "rel_path_basename": it.rel_path.rsplit("/", 1)[-1],
            "recording_id":  it.recording_id,
            "duplicate_of":  it.duplicate_of,
            "duplicate_basename": basenames.get(it.duplicate_of),
        } for it in dupe_items]
    payload["duplicates"] = duplicates

    return jsonify(payload)


def _get_run_or_404(run_id):
    run = db.session.get(BulkIngestRun, run_id)
    if run is None:
        return None, (jsonify({"error": "run not found"}), 404)
    return run, None


@bp.route("/<int:run_id>/pause", methods=["POST"])
@admin_required
def pause(run_id):
    run, err = _get_run_or_404(run_id)
    if err:
        return err
    if run.status == "running":
        bulk_ingest_run.pause_run(run)
    return jsonify(_serialize_run(run))


@bp.route("/<int:run_id>/resume", methods=["POST"])
@admin_required
def resume(run_id):
    run, err = _get_run_or_404(run_id)
    if err:
        return err
    if run.status == "paused":
        bulk_ingest_run.resume_run(run)
    return jsonify(_serialize_run(run))


@bp.route("/<int:run_id>/items", methods=["GET"])
@admin_required
def items(run_id):
    run, err = _get_run_or_404(run_id)
    if err:
        return err

    status = request.args.get("status")
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(500, max(1, request.args.get("per_page", 100, type=int)))

    q = db.session.query(BulkIngestItem).filter(BulkIngestItem.run_id == run_id)
    # "done" is a pseudo-status, not a real one (spec chunk 5e, 2026-09-27):
    # every item that has left pending/in_progress, for the log's infinite
    # scroll. Newest first there (id desc) -- the log itself reads newest
    # first -- unlike the plain per-status filter below, which keeps the
    # original id-asc order every other caller of this endpoint already
    # relies on.
    if status == "done":
        q = q.filter(BulkIngestItem.status.in_(_DONE_STATUSES))
        order = BulkIngestItem.id.desc()
    elif status and status != "all":
        # "all" (2026-09-27, the progress/log table redesign) is every status,
        # pending and in_progress included, ordered like the rest -- id
        # ascending, discovery order. The default (no status param) already
        # behaved this way; "all" just gives it a name callers can ask for
        # explicitly rather than relying on the absence of a filter.
        q = q.filter(BulkIngestItem.status == status)
        order = BulkIngestItem.id.asc()
    else:
        order = BulkIngestItem.id.asc()
    total = q.count()
    rows = (q.order_by(order)
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all())

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [{
            "id":           it.id,
            "rel_path":     it.rel_path,
            "status":       it.status,
            "reason":       it.reason,
            "detail":       it.detail,
            "kind":         it.kind,
            "format":       it.format,
            "recording_id": it.recording_id,
            "duplicate_of": it.duplicate_of,
            "updated_at":   it.updated_at.isoformat() if it.updated_at else None,
            **_item_meta_fields(it),
        } for it in rows],
    })


def _item_meta_fields(it):
    """
    Flatten BulkIngestItem.meta (a small JSON blob written at extraction
    time, app/utils/bulk_ingest_run.py::process) into the fields the
    2026-09-27 progress/log page's expand panel shows -- artist, date_text,
    venue, location (city/state/country joined, same rule as app.js's
    fmtLocation), source, title. Every field is None when meta is None
    (extract() never ran for this item, e.g. an already-in-library skip).
    """
    meta = {}
    if it.meta:
        try:
            meta = json.loads(it.meta) or {}
        except ValueError:
            meta = {}
    city, state, country = meta.get("city"), meta.get("state"), meta.get("country")
    if city and state:
        location = f"{city}, {state}"
    elif city and country:
        location = f"{city}, {country}"
    else:
        location = city or state or country or None
    return {
        "artist":    meta.get("artist"),
        "date_text": meta.get("date_text"),
        "venue":     meta.get("venue"),
        "location":  location,
        "source":    meta.get("source"),
        "lineage":   meta.get("lineage"),
        "title":     meta.get("title"),
        # Metadata band for the unified ingest queue table (2026-09-27) --
        # computed once at extraction time (bulk_ingest_run.py::process),
        # never here, so listing a page of items is still one query.
        "meta_band": meta.get("meta_band"),
    }
