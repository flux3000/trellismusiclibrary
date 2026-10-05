"""
api/bulk_ingest.py -- Bulk Ingest control surface (spec chunk 5d).

Routes:
    POST /api/bulk-ingest/start           start (or return) the run for a
                                          path (JSON {"path"}, default
                                          LIBRARY_ROOT); runs queue, small
                                          runs first
    GET  /api/bulk-ingest/runs            every unfinished run
    GET  /api/bulk-ingest/current         status/counts/duplicates/progress
                                          for it -- also `now` (in-progress
                                          folder), `upcoming` (next 8 pending
                                          folders) and `recent` (last 12
                                          ingested, for the progress view's
                                          Now/Up next/Recently added rows)
    POST /api/bulk-ingest/<id>/pause      pause a running run
    POST /api/bulk-ingest/<id>/resume     resume a paused run
    POST /api/bulk-ingest/runs/<id>/reset empty the run's Queue (everything not
                                          yet imported) and close the run;
                                          files are never touched
    GET  /api/bulk-ingest/<id>/items      paged item list, filterable by
                                          status; status=all is every item
                                          including pending/in_progress, id
                                          ascending (discovery order), for the
                                          progress table; status=done is a
                                          pseudo-filter (not pending/
                                          in_progress), newest first, for the
                                          old log's infinite scroll

    POST /api/bulk-ingest/items/<id>/ingest       ingest a ready item, or a
                                          review-flagged one ("Ingest anyway");
                                          queued to the worker, 202
    POST /api/bulk-ingest/runs/<id>/ingest-ready  queue every ready item, 202
    POST /api/bulk-ingest/items/<id>/move         {"dest"}: send a bring-in item's
                                          folder to Backlog/Workshop, 200
    POST /api/bulk-ingest/items/<id>/reanalyze    look at a folder again (after a
                                          conversion changed it), 202
    POST /api/bulk-ingest/runs/<id>/convert-unsupported
                                          convert every unsupported-format
                                          Queue row to FLAC, one at a time on
                                          the server, 202
    PUT  /api/bulk-ingest/runs/<id>/applied       store the run's blanket
                                          "applies to every recording" values

POST /start also takes "mode": "auto" (Import Automatically) or "hold"
(Review First); default auto inside the library, hold anywhere else.

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
import os

from flask import Blueprint, current_app, jsonify, request
from flask_login import login_required, current_user
from sqlalchemy import func, or_

from app.extensions import db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.quality import RecordingQuality
from app.models.recording import Recording
from app.models.performance import Performance
from app.models.artist import Artist
from app.utils.authz import admin_required
from app.utils.format import format_partial_date
from app.utils import bulk_ingest_run
from app.utils.quality import verdict_band
from app.utils.paths import is_within, within_import_roots

# Not pending or in_progress -- the pseudo-status the log's infinite scroll
# filters on (GET .../items?status=done). A finished item, whichever of the
# four terminal statuses it landed on. Reason -> result-phrase mapping lives
# client-side (app.js), same phrases the done-summary's reason breakdown
# already uses.
_DONE_STATUSES = ("ingested", "review", "skipped", "failed", "ready", "moved")

bp = Blueprint("bulk_ingest", __name__)

_ITEM_STATUSES = ("pending", "in_progress", "ingested", "review", "skipped", "failed",
                  "ready", "moved")

# Keys a run's blanket "applies to every recording below" values may carry.
_APPLIED_KEYS = ("artist", "venue", "city", "state", "country", "event", "stage",
                 "source", "source_tag", "lineage", "notes")


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
    # bulk_ingest (app/api/ingest.py::_handle_audio) -- "scorable" is every
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
        # Derived, never stored: a root inside LIBRARY_ROOT is cataloged in
        # place, anything else is brought in.
        "placement":       "in_place" if bulk_ingest_run.is_in_library(run.root) else "bring_in",
        "status":          run.status,
        "mode":            run.mode,
        "applied":         bulk_ingest_run.applied_values(run) or None,
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


def _rel_name(rel_path, run):
    """Display name for an item: its folder's basename. An outside single-show
    run has one item with rel_path "." -- that folder is the run root."""
    if rel_path in (".", ""):
        return os.path.basename(run.root.rstrip("/")) or run.root
    return rel_path.rsplit("/", 1)[-1]


def _item_name(it, run):
    return _rel_name(it.rel_path, run)


def _current_run():
    """The run named by an optional ?run_id= (or JSON run_id), else the most
    recently started run of any status -- so /current still has something to
    report right after a run finishes."""
    run_id = request.args.get("run_id", type=int)
    if run_id is None:
        run_id = (request.get_json(silent=True) or {}).get("run_id")
    if run_id is not None:
        return db.session.get(BulkIngestRun, run_id)
    # Prefer a run the person still has something to do with (unfinished, or a
    # Review First queue with ready/review items) over a newer finished one.
    listed = bulk_ingest_run.listed_runs()
    if listed:
        return listed[-1]
    return (db.session.query(BulkIngestRun)
            .order_by(BulkIngestRun.id.desc())
            .first())


@bp.route("/start", methods=["POST"])
@admin_required
def start():
    library_root = os.path.realpath(str(current_app.config["LIBRARY_ROOT"]))
    raw = (request.get_json(silent=True) or {}).get("path")
    if raw in (None, ""):
        root = library_root
    else:
        if not isinstance(raw, str):
            return jsonify({"error": "path must be a string"}), 400
        root = os.path.realpath(raw)
        if not os.path.isdir(root):
            return jsonify({"error": "Folder not found"}), 400
        if not within_import_roots(root):
            return jsonify({"error": "Folder is outside the permitted import roots"}), 400
        # A run over an ancestor of the library would try to bring the
        # library into itself.
        if root != library_root and is_within(library_root, root):
            return jsonify({"error": "This folder contains the Library"}), 400
        from app.utils.download_queue import downloading_here
        if downloading_here(root):
            return jsonify({"error": "This folder is still downloading."}), 409
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode not in (None, "auto", "hold"):
        return jsonify({"error": "mode must be 'auto' or 'hold'"}), 400
    run = bulk_ingest_run.start_run(root, mode)
    return jsonify(_serialize_run(run))


@bp.route("/runs", methods=["GET"])
@login_required
def runs():
    out = []
    listed = bulk_ingest_run.listed_runs()
    for run in listed:
        p = _serialize_run(run)
        out.append({k: p[k] for k in ("id", "root", "placement", "status", "mode", "counts")})
    # Distinct folders waiting on a person across every listed run. Summing
    # per-run counts double-counts a folder that sits in two queues (a
    # one-folder Downloads run and a later run over all of Downloads), which
    # put "4" on the Add Recordings badge for two rows (2026-10-02).
    waiting = set()
    ids = [r.id for r in listed]
    if ids:
        for it in (db.session.query(BulkIngestItem)
                   .filter(BulkIngestItem.run_id.in_(ids),
                           BulkIngestItem.status.in_(("ready", "review")))
                   .all()):
            waiting.add(bulk_ingest_run.item_abs_path(it))
    return jsonify({"runs": out, "waiting": len(waiting)})


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
    payload["current"] = _item_name(current_item, run) if current_item else None
    payload["now"] = payload["current"]

    # "Up next" (spec chunk 5e, 2026-09-27): the next eight pending items in
    # the order process() will actually reach them (id asc), basenames only.
    upcoming_rows = (db.session.query(BulkIngestItem.rel_path)
                     .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "pending")
                     .order_by(BulkIngestItem.id.asc())
                     .limit(8)
                     .all())
    payload["upcoming"] = [_rel_name(rp, run) for (rp,) in upcoming_rows]

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
            "rel_path_basename": _item_name(it, run),
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


@bp.route("/runs/<int:run_id>/reset", methods=["POST"])
@admin_required
def reset(run_id):
    """Reset Queue: drop every not-yet-imported item and close the run, so it
    is no longer listed and is not resumed on boot. Never touches files."""
    run, err = _get_run_or_404(run_id)
    if err:
        return err
    removed = bulk_ingest_run.reset_queue(run)
    return jsonify({"removed": removed, "run": _serialize_run(run)})


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
    # ?ids=1,2,3 -- the poll's cheap path: just the rows that can still change
    # (pending / in progress / requested), so a tick over a big queue costs a
    # handful of rows instead of re-serializing every loaded page.
    raw_ids = request.args.get("ids")
    if raw_ids:
        want = [int(x) for x in raw_ids.split(",") if x.strip().isdigit()][:500]
        rows = (q.filter(BulkIngestItem.id.in_(want))
                .order_by(BulkIngestItem.id.asc()).all())
        from app.utils.serialize import batch_recording_image_urls
        image_urls = batch_recording_image_urls([it.recording_id for it in rows if it.recording_id])
        return jsonify({"total": len(rows), "page": 1, "per_page": len(rows),
                        "items": [_serialize_item(it, image_urls) for it in rows]})
    # "done" is a pseudo-status, not a real one (spec chunk 5e, 2026-09-27):
    # every item that has left pending/in_progress, for the log's infinite
    # scroll. Newest first there (id desc) -- the log itself reads newest
    # first -- unlike the plain per-status filter below, which keeps the
    # original id-asc order every other caller of this endpoint already
    # relies on.
    if status == "done":
        q = q.filter(BulkIngestItem.status.in_(_DONE_STATUSES))
        order = BulkIngestItem.id.desc()
    elif status == "queue":
        # Import page tabs (2026-10-01): everything not yet in the library --
        # pending, in progress, ready, review, skipped, failed. Discovery
        # order. 'moved' items went to Backlog/Workshop: terminal, not queued.
        q = q.filter(BulkIngestItem.status.notin_(("ingested", "moved")))
        order = BulkIngestItem.id.asc()
    elif status in ("live", "album"):
        # The two completed tabs: ingested, split by kind. A NULL kind on an
        # ingested item counts as live, the model default.
        q = q.filter(BulkIngestItem.status == "ingested")
        if status == "album":
            q = q.filter(BulkIngestItem.kind == "studio")
        else:
            q = q.filter(or_(BulkIngestItem.kind.is_(None), BulkIngestItem.kind != "studio"))
        order = BulkIngestItem.id.asc()
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
    if status == "review":
        # One row per unconfirmed act first (settle it once and the rest of its rows clear),
        # then the rest in discovery order.
        ordered = bulk_ingest_run.lead_first(
            q.with_entities(BulkIngestItem.id, BulkIngestItem.meta)
            .order_by(order).all())
        by_id = {it.id: it for it in
                 q.filter(BulkIngestItem.id.in_(ordered[(page - 1) * per_page:page * per_page])).all()}
        rows = [by_id[i] for i in ordered[(page - 1) * per_page:page * per_page] if i in by_id]
    else:
        rows = (q.order_by(order)
                .offset((page - 1) * per_page)
                .limit(per_page)
                .all())

    # Recording artwork for imported rows (2026-10-01): the completed tabs
    # show it at the left of each row. One batched query, not one per row.
    from app.utils.serialize import batch_recording_image_urls
    image_urls = batch_recording_image_urls([it.recording_id for it in rows if it.recording_id])

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [_serialize_item(it, image_urls) for it in rows],
    })


def _serialize_item(it, image_urls=None):
    return {
        "id":           it.id,
        "rel_path":     it.rel_path,
        "status":       it.status,
        "reason":       it.reason,
        "detail":       it.detail,
        "kind":         it.kind,
        "format":       it.format,
        "recording_id": it.recording_id,
        "image_url":    (image_urls or {}).get(it.recording_id) if it.recording_id else None,
        "duplicate_of": it.duplicate_of,
        "ingest_requested": bool(it.ingest_requested),
        # Live convert progress ({done,total,current}) while a job runs on this
        # folder, else None -- how Convert All's rows show their state.
        "converting":   _converting_progress(it),
        # Where the folder actually is. rel_path is relative to the library
        # root for an in-library run and to the run root otherwise, and is "."
        # for an outside single-show folder, so the client cannot rebuild it.
        "abs_path":     bulk_ingest_run.item_abs_path(it),
        "updated_at":   it.updated_at.isoformat() if it.updated_at else None,
        **_item_meta_fields(it),
    }


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
        "track_count": meta.get("track_count"),
        "tracks":      meta.get("tracks") or [],
        # Metadata band for the unified ingest queue table (2026-09-27) --
        # computed once at extraction time (bulk_ingest_run.py::process),
        # never here, so listing a page of items is still one query.
        "meta_band": meta.get("meta_band"),
        # Pre-ingest Listening Quality (Review First, live items only).
        "listening_quality": meta.get("listening_quality"),
        "sound_band": verdict_band(meta.get("listening_quality")),
    }


# ── Review First: person-driven actions ────────────────────────────────────
# Ingesting is always done by the worker (the one thing that touches folders):
# these routes only flag items as requested, wake the worker and return, so no
# request is held open for a long copy. Outcomes land on the item; a flagged
# item that still cannot be ingested (no artist, exact duplicate) comes back
# as review / skipped with its reason.

def _converting_progress(it):
    from app.api.quality import convert_progress
    if it.status not in ("ready", "review"):
        return None
    prog = convert_progress(bulk_ingest_run.item_abs_path(it))
    if prog is None and it.id in _CONVERT_ALL_QUEUED:
        # Waiting its turn in Convert All: still "converting" to the page.
        prog = {"done": 0, "total": 0, "current": None}
    return prog


def _unsupported(it):
    """True for a Queue row held back for audio Trellis does not import
    (WAV/AIFF/SHN/APE/WV). Such a row can only be converted or moved."""
    return it.status == "review" and "unsupported_format" in (it.reason or "").split(",")


def _converting_error(items):
    """409 response when any of `items` sits in a folder being converted
    (ingest would move files out from under the converter), else None. Checked
    under FS_LOCK like the downloading check."""
    from app.api.quality import converting_here
    from app.utils import download_queue
    with download_queue.FS_LOCK:
        if any(converting_here(bulk_ingest_run.item_abs_path(it)) for it in items):
            return jsonify({"error": "This folder is still converting."}), 409
    return None


def _request_ingest(run, items):
    for it in items:
        it.ingest_requested = True
    if run.status == "done":
        # A finished run has no worker driving it; bring it back to life. The
        # worker marks it done again once the requests are worked off.
        run.status = "running"
        run.finished_at = None
    db.session.commit()
    bulk_ingest_run._start_worker()


@bp.route("/items/<int:item_id>/ingest", methods=["POST"])
@admin_required
def ingest_item(item_id):
    """Ingest one ready item, or a review-flagged one ("Ingest anyway")."""
    item = db.session.get(BulkIngestItem, item_id)
    if item is None:
        return jsonify({"error": "item not found"}), 404
    if item.status not in ("ready", "review"):
        return jsonify({"error": f"item is {item.status}, not ready or review"}), 409
    if _unsupported(item):
        # Not even "Ingest anyway": unsupported audio is converted first.
        return jsonify({"error": "Unsupported format"}), 409
    run = item.run
    if run.status == "paused":
        return jsonify({"error": "run is paused"}), 409
    err = _converting_error([item])
    if err:
        return err
    _request_ingest(run, [item])
    return jsonify(_serialize_item(item)), 202


@bp.route("/runs/<int:run_id>/ingest-ready", methods=["POST"])
@admin_required
def ingest_ready(run_id):
    """Queue every ready item of a run for ingest. Review-flagged items are
    deliberately left alone."""
    run, err = _get_run_or_404(run_id)
    if err:
        return err
    if run.status == "paused":
        return jsonify({"error": "run is paused"}), 409
    ready = (db.session.query(BulkIngestItem)
             .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "ready")
             .all())
    ready = [it for it in ready if not _unsupported(it)]
    if ready:
        err = _converting_error(ready)
        if err:
            return err
        _request_ingest(run, ready)
    return jsonify({"queued": len(ready), "run": _serialize_run(run)}), 202


@bp.route("/runs/<int:run_id>/applied", methods=["PUT"])
@admin_required
def set_applied(run_id):
    """
    Store the run's blanket "applies to every recording below" values: a JSON
    object of the non-empty fields to impose (artist, venue [+ venue_id], city,
    state, country, event, stage, source, source_tag, lineage, notes). They overwrite the scan's
    inference at ingest, in either mode. An empty object (or null) clears them.
    """
    run, err = _get_run_or_404(run_id)
    if err:
        return err
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    applied = {}
    for key in _APPLIED_KEYS:
        v = body.get(key)
        if v is None:
            continue
        if not isinstance(v, str):
            return jsonify({"error": f"{key} must be a string"}), 400
        v = v.strip()
        if v:
            applied[key] = v
    vid = body.get("venue_id")
    if applied.get("venue") and vid is not None:
        if isinstance(vid, bool) or not isinstance(vid, int):
            return jsonify({"error": "venue_id must be an integer"}), 400
        applied["venue_id"] = vid
    run.applied_json = json.dumps(applied) if applied else None
    db.session.commit()
    return jsonify(_serialize_run(run))


@bp.route("/items/<int:item_id>/move", methods=["POST"])
@admin_required
def move_item(item_id):
    """
    Send a waiting item's folder to Backlog or Workshop instead of ingesting it,
    through the same guarded move Review & Ingest's Move uses (app/api/quality.py
    ::move_folder_to_triage, which also refuses a folder still downloading and
    takes FS_LOCK). Bring-in runs only: an in-place folder is the person's own
    library and Trellis never moves it.
    """
    from app.api.quality import move_folder_to_triage

    item = db.session.get(BulkIngestItem, item_id)
    if item is None:
        return jsonify({"error": "item not found"}), 404
    if bulk_ingest_run.is_in_library(item.run.root):
        return jsonify({"error": "Items in the library are never moved"}), 409
    if item.status not in ("ready", "review"):
        return jsonify({"error": f"item is {item.status}, not ready or review"}), 409
    # Already handed to the worker for ingest: it moves the folder into the
    # library without FS_LOCK, so a Move now would race it for the same files.
    if item.ingest_requested:
        return jsonify({"error": "item is already queued for import"}), 409
    err = _converting_error([item])
    if err:
        return err
    dest = ((request.get_json(silent=True) or {}).get("dest") or "").strip().lower()
    payload, status = move_folder_to_triage(bulk_ingest_run.item_abs_path(item), dest)
    if status != 200:
        return jsonify(payload), status
    item.status = "moved"
    item.ingest_requested = False
    db.session.commit()
    return jsonify({**_serialize_item(item), "moved_to": payload["moved_to"]})


@bp.route("/items/<int:item_id>/reanalyze", methods=["POST"])
@admin_required
def reanalyze_item(item_id):
    """Put a ready/review item back to pending so the worker reads its folder
    again -- used after Convert, which changes what is in the folder."""
    item = db.session.get(BulkIngestItem, item_id)
    if item is None:
        return jsonify({"error": "item not found"}), 404
    if item.status not in ("ready", "review"):
        return jsonify({"error": f"item is {item.status}, not ready or review"}), 409
    run = item.run
    if run.status == "paused":
        return jsonify({"error": "run is paused"}), 409
    _reset_for_reanalysis(item)
    db.session.commit()
    bulk_ingest_run._start_worker()
    return jsonify(_serialize_item(item)), 202


def _reset_for_reanalysis(item):
    """Put an item back to pending so the worker reads its folder again."""
    run = item.run
    item.status = "pending"
    item.reason = None
    item.detail = None
    item.meta = None
    item.format = None
    item.ingest_requested = False
    if run.status == "done":
        run.status = "running"
        run.finished_at = None


# ── Convert All ─────────────────────────────────────────────────────────────
# Runs the same convert job a row's Convert uses, but one folder at a time on a
# server thread, so the work survives the page being closed and two decodes
# never fight over the disk. Each finished row goes back to pending (re-scanned
# by the worker) and, if its files are now FLAC, becomes importable.
_CONVERT_ALL_ACTIVE = set()    # run ids with a Convert All thread running
_CONVERT_ALL_QUEUED = set()    # item ids Convert All has not finished yet


def _convert_all_worker(app, run_id, item_ids):
    from app.api.quality import (ConvertRefused, register_convert_job,
                                 _run_convert_job, _CONVERT_JOBS)
    from app.utils import quality_store as qs
    try:
        with app.app_context():
            for item_id in item_ids:
                if item_id not in _CONVERT_ALL_QUEUED:
                    continue
                run = db.session.get(BulkIngestRun, run_id)
                if run is None or run.status == "paused":
                    break
                item = db.session.get(BulkIngestItem, item_id)
                if item is None or not _unsupported(item):
                    _CONVERT_ALL_QUEUED.discard(item_id)
                    continue
                folder = bulk_ingest_run.item_abs_path(item)
                try:
                    job_id, what, ffmpeg = register_convert_job(folder)
                except ConvertRefused as e:
                    # Downloading or already converting: leave the row as it is.
                    item.detail = str(e)
                    db.session.commit()
                    _CONVERT_ALL_QUEUED.discard(item_id)
                    continue
                _run_convert_job(job_id, app, folder, ffmpeg, what["exts"])
                job = _CONVERT_JOBS.pop(job_id, {})
                if job.get("status") == "error":
                    item.detail = job.get("error")
                    db.session.commit()
                    _CONVERT_ALL_QUEUED.discard(item_id)
                    continue
                # The staging row describes files that no longer exist.
                try:
                    row = qs.get_staging(job["folder"])
                    if row is not None and row.recording_id is None:
                        db.session.delete(row)
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    item = db.session.get(BulkIngestItem, item_id)
                _reset_for_reanalysis(item)
                db.session.commit()
                _CONVERT_ALL_QUEUED.discard(item_id)
                bulk_ingest_run._start_worker()
    finally:
        _CONVERT_ALL_QUEUED.difference_update(item_ids)
        _CONVERT_ALL_ACTIVE.discard(run_id)
        try:
            db.session.remove()
        except Exception:  # noqa: BLE001
            pass


@bp.route("/runs/<int:run_id>/convert-unsupported", methods=["POST"])
@admin_required
def convert_unsupported(run_id):
    """Queue every unsupported-format Queue row of a run for conversion to
    FLAC. Rows whose folder is still downloading or already converting are
    left alone."""
    import threading
    from app.api.quality import converting_here
    from app.utils import download_queue

    run, err = _get_run_or_404(run_id)
    if err:
        return err
    if run.status == "paused":
        return jsonify({"error": "run is paused"}), 409
    rows = (db.session.query(BulkIngestItem)
            .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "review")
            .order_by(BulkIngestItem.id.asc()).all())
    rows = [it for it in rows if _unsupported(it)]
    if rows and run_id in _CONVERT_ALL_ACTIVE:
        return jsonify({"error": "This folder is still converting."}), 409
    with download_queue.FS_LOCK:
        busy_dl = [it for it in rows
                   if download_queue.downloading_here(bulk_ingest_run.item_abs_path(it))]
        ready = [it for it in rows if it not in busy_dl
                 and not converting_here(bulk_ingest_run.item_abs_path(it))]
    if rows and not ready:
        msg = ("This folder is still downloading." if busy_dl
               else "This folder is still converting.")
        return jsonify({"error": msg}), 409
    if ready:
        _CONVERT_ALL_ACTIVE.add(run_id)
        _CONVERT_ALL_QUEUED.update(it.id for it in ready)
        threading.Thread(
            target=_convert_all_worker,
            args=(current_app._get_current_object(), run_id, [it.id for it in ready]),
            daemon=True,
        ).start()
    return jsonify({"queued": len(ready), "run": _serialize_run(run)}), 202
