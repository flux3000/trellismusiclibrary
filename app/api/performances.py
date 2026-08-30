"""
api/performances.py — Performance endpoints.

A Performance belongs to one Performer (the act). `members` (back-compat key)
and `personnel` are the RESOLVED show-level lineup — act roster with stint
bounds applied, plus guests, or an explicit per-show list — never the raw,
unfiltered act roster. See app/utils/personnel.py::resolve_performance_personnel.
The act roster itself is only ever edited via PUT /api/performers/<id>.
"""

from flask import Blueprint, current_app, jsonify, request
from flask_login import login_required

from app.extensions import db
from app.models.performance import Performance
from app.models.performer import Performer
from app.models.performance_personnel import PerformancePersonnel
from app.utils.format import format_partial_date
from app.utils.serialize import recording_summary
from app.utils.performers import resolve_or_create_performer
from app.utils.personnel import (
    resolve_performance_personnel, sync_performance_personnel,
    set_performance_personnel_mode,
)
from app.utils.pruning import (
    prune_performer_if_orphaned, prune_venue_if_orphaned, prune_event_if_orphaned,
)
from app.utils.folder_naming import rename_recording_folder

bp = Blueprint("performances", __name__)


@bp.route("/")
@login_required
def list_performances():
    performer_id = request.args.get("performer_id", type=int)
    year         = request.args.get("year",         type=int)

    q = db.session.query(Performance)
    if performer_id:
        q = q.filter(Performance.performer_id == performer_id)
    if year:
        q = q.filter(Performance.start_year == year)

    perfs = q.order_by(Performance.start_year.desc()).limit(200).all()
    return jsonify([
        {
            "id":        p.id,
            "performer": p.performer.name,
            "date":      format_partial_date(p.start_year, p.start_month, p.start_day),
            "venue":     p.venue.name if p.venue else None,
            "city":      p.venue.city  if p.venue else p.city,
            "state":     p.venue.state if p.venue else p.state,
        }
        for p in perfs
    ])


@bp.route("/<int:performance_id>")
@login_required
def get_performance(performance_id):
    p = db.session.get(Performance, performance_id)
    if not p:
        return jsonify({"error": "Not found"}), 404

    v = p.venue
    resolved = resolve_performance_personnel(p)
    # Performer photo + genre colour ride along on the performance detail so the
    # Record Header can draw its avatar circle without a second round trip. Same
    # id the card serializer sends (serialize._primary_image_id), so the header,
    # the cards and the Performer page hero can never show different faces.
    from app.utils.serialize import _primary_image_id
    _perf_genre = getattr(p.performer, "genre", None)
    return jsonify({
        "id":           p.id,
        "performer_id": p.performer_id,
        "performer":    p.performer.name,
        "performer_image_id":   _primary_image_id(p.performer),
        "performer_genre_color": _perf_genre.color if _perf_genre else None,
        # Back-compat shape (id/name pairs) for the existing recording-page
        # Artists pill row — now the RESOLVED show lineup (act roster with
        # stint bounds applied, plus guests, or the explicit list), not the
        # raw act roster. Existing UI needs zero changes to pick this up.
        "members":         [{"id": r["artist_id"], "name": r["name"]} for r in resolved],
        # Full detail incl. instrument/is_guest/note/source/row-id, for the
        # Phase 2 show-personnel UI.
        "personnel":       resolved,
        "personnel_mode":  p.personnel_mode,
        "title":        p.title,
        "stage":        p.stage,
        "start_year":   p.start_year,
        "start_month":  p.start_month,
        "start_day":    p.start_day,
        "end_year":     p.end_year,
        "end_month":    p.end_month,
        "end_day":      p.end_day,
        "venue_id":     v.id      if v else None,
        "venue_name":   v.name    if v else None,
        "city":         v.city    if v else p.city,
        "state":        v.state   if v else p.state,
        "country":      v.country if v else p.country,
        "event_id":     p.event_id,
        "event_name":   p.event.name if p.event else None,
        "notes":        p.notes,
        "recordings":   [recording_summary(r) for r in p.recordings],
    })


@bp.route("/", methods=["POST"])
@login_required
def create_performance():
    data = request.get_json()
    if not data.get("performer_id"):
        return jsonify({"error": "performer_id is required"}), 400
    performer = db.session.get(Performer, data["performer_id"])
    if not performer:
        return jsonify({"error": "performer not found"}), 404
    p = Performance(
        performer_id = data["performer_id"],
        venue_id     = data.get("venue_id"),
        event_id     = data.get("event_id"),
        title        = data.get("title"),
        stage        = data.get("stage"),
        start_year   = data.get("start_year"),
        start_month  = data.get("start_month"),
        start_day    = data.get("start_day"),
        end_year     = data.get("end_year"),
        end_month    = data.get("end_month"),
        end_day      = data.get("end_day"),
        city         = data.get("city"),
        state        = data.get("state"),
        country      = data.get("country"),
        notes        = data.get("notes"),
        # New performances start in the act's default resolution mode.
        personnel_mode = performer.default_personnel_mode,
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({"id": p.id}), 201


@bp.route("/<int:performance_id>", methods=["PUT"])
@login_required
def update_performance(performance_id):
    p = db.session.get(Performance, performance_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json()

    # Reassign the Performer when a new name is supplied and differs from the
    # current one. Scoped to this performance; the old performer is pruned if it
    # is left with no performances.
    reassigned = None
    new_name = (data.get("performer_name") or "").strip()
    if new_name and new_name.lower() != p.performer.name.lower():
        old_performer_id = p.performer_id
        performer = resolve_or_create_performer(new_name)
        p.performer_id = performer.id
        db.session.flush()
        prune_performer_if_orphaned(old_performer_id)
        reassigned = performer.name

    # Manual inherit/explicit toggle — applied BEFORE the members diff below,
    # so a same-request combo of {personnel_mode, members} lands on the new
    # mode's baseline (e.g. flip to explicit, which snapshots the current
    # lineup, then edit from there) rather than the old mode's.
    if data.get("personnel_mode") is not None:
        if data["personnel_mode"] not in ("inherit", "explicit"):
            return jsonify({"error": "personnel_mode must be 'inherit' or 'explicit'"}), 400
        set_performance_personnel_mode(p, data["personnel_mode"])

    # SHOW-level personnel — who played THIS performance. Distinct from the
    # act roster (which is edited only via PUT /api/performers/<id> on the
    # Performer page). This used to call set_performer_members(p.performer,
    # ...), silently rewriting the act's GLOBAL roster from a single show's
    # pill-row edit — that was the actual bug the Per-Show Personnel design
    # doc set out to fix. Now it only ever touches performance_personnel rows
    # scoped to this performance; see utils/personnel.py.
    #
    # Members/Guests two-row UI (2026-07-22): 'members' and 'guests' are two
    # independent name lists, not one list with guest-vs-member inferred by
    # diffing. Either key can be omitted (not sent) to leave that row
    # untouched — the frontend always sends both together on any edit, so in
    # practice this only matters for API callers that don't.
    if "members" in data or "guests" in data:
        sync_performance_personnel(p, data.get("members"), data.get("guests"))

    # Capture the outgoing venue/event before the generic field loop
    # overwrites them, so a repoint that leaves either with zero
    # performances prunes the now-empty row -- same trip-wire as the
    # Performer reassignment above via prune_performer_if_orphaned.
    old_venue_id = p.venue_id
    old_event_id = p.event_id

    for f in ["title", "stage", "start_year", "start_month", "start_day",
              "end_year", "end_month", "end_day", "venue_id", "event_id",
              "city", "state", "country", "notes"]:
        if f in data:
            setattr(p, f, data[f])

    db.session.flush()
    pending_venue_image_cleanup = []
    if "venue_id" in data and old_venue_id != p.venue_id:
        _, pending_venue_image_cleanup = prune_venue_if_orphaned(old_venue_id)
    if "event_id" in data and old_event_id != p.event_id:
        prune_event_if_orphaned(old_event_id)

    # Folder name follows the metadata (decided 2026-07-25, built 2026-08-09)
    # — date, venue, location and a performer reassignment all feed
    # build_folder_name(), and a Performance can hold several Recordings
    # (SBD + AUD of the same show), each with its own folder to keep in sync.
    # Non-fatal per-recording: one folder rename failing (e.g. its directory
    # already moved out from under it) must not stop the others or block the
    # metadata commit below.
    library_root   = current_app.config.get("LIBRARY_ROOT", "")
    rename_errors  = [err for err in
                       (rename_recording_folder(r, library_root) for r in p.recordings)
                       if err]

    db.session.commit()

    # Deferred from prune_venue_if_orphaned above: only unlink photo files
    # for a deleted venue now that the row deletion has actually committed.
    for _path in pending_venue_image_cleanup:
        try:
            if _path.exists():
                _path.unlink()
        except OSError:
            pass

    resp = {"id": p.id, "reassigned_to": reassigned}
    if rename_errors:
        resp["folder_rename_errors"] = rename_errors
    return jsonify(resp)


@bp.route("/<int:performance_id>/personnel/<int:personnel_id>", methods=["PUT"])
@login_required
def update_personnel_row(performance_id, personnel_id):
    """
    Edit instrument/note on one show-level personnel row. Only applies to
    guest/explicit-sourced rows (the ones that actually have a
    performance_personnel row of their own) — a purely inherited person has
    no row here to edit; adding an instrument for them would first mean
    adding them as an explicit entry, not a plain metadata edit.
    """
    row = db.session.get(PerformancePersonnel, personnel_id)
    if not row or row.performance_id != performance_id:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    for f in ["instrument", "note"]:
        if f in data:
            setattr(row, f, data[f])
    db.session.commit()
    return jsonify({"id": row.id})
