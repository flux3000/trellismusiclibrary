"""
api/recordings.py — Recording endpoints.

Routes:
  GET  /api/recordings/<id>                 full recording detail (incl. analysis)
  GET  /api/recordings/favorites            starred recordings, for the sidebar
  POST /api/recordings/scan                 scan a folder, return suggestions (no DB write)
  PUT  /api/recordings/<id>                update recording metadata
  POST /api/recordings/<id>/write-tags     write Vorbis comments to FLAC files
  POST /api/recordings/<id>/info-file      save info-file text (DB, + disk if a .txt exists)
  DELETE /api/recordings/<id>[?delete_files=1]  delete the record, optionally the folder too
  POST /api/recordings/<id>/move           move the folder out to Workshop/Backlog (unpublish)
  POST /api/recordings/<id>/reprocess      re-run Librosa analysis on all tracks
  POST /api/recordings/<id>/verify-checksums  (re-)validate fingerprint checksums
"""

import json as _json
import os
import random
import subprocess
from datetime import datetime, timezone, date as _date
from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.recording import Recording, RecordingFingerprint
from app.models.collection import CollectionRecording
from app.models.recording_event import RecordingEvent
from app.models.track import Track
from app.models.performance import Performance
from app.models.performer import Performer
from app.models.play_log import PlayLog
from app.models.venue import Venue
from app.utils.ingest import (build_scan_payload, write_flac_tags, read_recording_tags)
from app.utils.folder_naming import rename_recording_folder
from app.utils.analysis import analyse_recording
from app.utils.pruning import prune_after_recording_delete
from app.utils.serialize import recording_row
from app.utils.paula import compute_paula_score
from app.utils.checksums import (
    discover_fingerprint_files, parse_checksum_file,
    match_entries_to_tracks, verify_track_checksum, FINGERPRINT_TYPE_PRIORITY,
)

bp = Blueprint("recordings", __name__)


# ── GET /api/recordings/recent ────────────────────────────────────────────────
# Virtual "Recently Added" view — not a stored grouping, just the N newest
# recordings by ingest timestamp. Always exactly correct, nothing to keep in sync.
#
# Shared by two callers: the List view's Recently Added table (no waveform —
# default, unaffected) and the Browse view's Recently Added card module
# (?waveform=1). Same endpoint, opt-in param, per the recording_row() design —
# see app/utils/serialize.py.

def _card_eager(query):
    """
    Eager-load everything `recording_row(card=True)` walks.

    Without this each card row lazy-loads Performance → Performer → Genre and
    → PerformerImage separately, which is an N+1 the moment a module shows more
    than a couple of cards. Defined once so every card-bearing endpoint loads
    the same set — the serializer's docstring names this as the caller's
    responsibility, and three copies of it would eventually disagree.
    """
    return query.options(
        selectinload(Recording.performance)
        .selectinload(Performance.performer)
        .selectinload(Performer.images),
        selectinload(Recording.performance)
        .selectinload(Performance.performer)
        .selectinload(Performer.genre),
    )


@bp.route("/recent")
@login_required
def recent_recordings():
    limit = request.args.get("limit", 50, type=int) or 50
    limit = max(1, min(limit, 200))
    # Paging, added 2026-08-23 so Recently Added can scroll indefinitely
    # instead of stopping at a hardcoded 50 (Ryan). Offset over keyset is fine
    # here: the ordering column is created_at, which does not change under the
    # reader, so a page boundary cannot skip or repeat a row mid-scroll.
    offset = max(0, request.args.get("offset", 0, type=int) or 0)
    waveform = request.args.get("waveform", "").lower() in ("1", "true", "yes")
    # `card=1` adds genre colour + primary image for Browse's Recently Added
    # row cards. Opt-in for the same reason as waveform: this endpoint also
    # backs the List view's flat table, which needs none of it.
    card = request.args.get("card", "").lower() in ("1", "true", "yes")
    query = Recording.query
    if waveform:
        query = query.options(selectinload(Recording.tracks).selectinload(Track.analysis))
    if card:
        query = _card_eager(query)
    # Reverse chronological by ingest time — most recently added first. This is
    # the module's whole premise, so it is the query's order, not something the
    # client re-sorts.
    recs = query.order_by(Recording.created_at.desc()).offset(offset).limit(limit).all()
    return jsonify([recording_row(r, waveform=waveform, card=card) for r in recs])


# ── GET /api/recordings/recommended ───────────────────────────────────────────
# Browse view's "Recommended" module (Library Browse View design spec,
# 2026-08-02). Randomly-selected high-quality (A/A+ only — not A-) recordings,
# weighted toward ones absent from play_log, diverse by Performer (hard rule)
# and Genre (soft preference, degrades silently while genre_id is
# NULL/absent). Seeded by date so picks are stable within a day; the client's
# "Show me three more" control advances `reroll` to get a fresh draw.

def _recommended_pool_query():
    return (
        Recording.query
        .filter(Recording.quality.in_(("A", "A+")))
        .options(
            selectinload(Recording.tracks).selectinload(Track.analysis),
            selectinload(Recording.performance),
        )
    )


def _genre_by_performer(performer_ids):
    """
    Best-effort {performer_id: genre_id} map for the diversity preference.
    Returns {} (no genre preference applied) if the query fails for any
    reason — in particular, `performer.genre_id` may not exist yet on the
    live DB (the genre migration had not been run as of this feature's
    build). Diversity-by-genre is a soft preference, never load-bearing, so
    failing this open (no genre data) rather than raising is the right call.
    """
    if not performer_ids:
        return {}
    try:
        return dict(
            db.session.query(Performer.id, Performer.genre_id)
            .filter(Performer.id.in_(performer_ids))
            .all()
        )
    except Exception:
        db.session.rollback()
        return {}


def _select_diverse(candidates, limit, perf_by_rec, genre_by_performer):
    """
    Greedily pick up to `limit` recordings from `candidates` (already ordered
    by preference — unplayed first). Never two picks share a Performer — a
    hard rule, no exception: if the pool doesn't have `limit` distinct
    Performers to offer, the draw comes back short rather than repeating one.
    Prefers distinct Genres too, but relaxes that if the pool can't support
    it (soft preference, degrades silently while genre_id is NULL/absent).
    """
    picks = []
    used_performers = set()
    used_genres = set()
    deferred = []   # distinct-performer, but genre already used this draw

    for r in candidates:
        if len(picks) >= limit:
            break
        pid = perf_by_rec.get(r.id)
        if pid is not None and pid in used_performers:
            continue
        gid = genre_by_performer.get(pid) if pid is not None else None
        if gid is not None and gid in used_genres:
            deferred.append((r, pid, gid))
            continue
        picks.append(r)
        if pid is not None:
            used_performers.add(pid)
        if gid is not None:
            used_genres.add(gid)

    if len(picks) < limit:
        for r, pid, gid in deferred:
            if len(picks) >= limit:
                break
            if pid is not None and pid in used_performers:
                continue
            picks.append(r)
            if pid is not None:
                used_performers.add(pid)

    return picks[:limit]


@bp.route("/recommended")
@login_required
def recommended_recordings():
    limit = request.args.get("limit", 3, type=int) or 3
    limit = max(1, min(limit, 12))
    reroll = request.args.get("reroll", 0, type=int) or 0

    pool = _card_eager(_recommended_pool_query()).all()
    if not pool:
        return jsonify([])

    pool_ids = [r.id for r in pool]
    played_ids = {
        rid for (rid,) in
        db.session.query(Track.recording_id)
        .join(PlayLog, PlayLog.track_id == Track.id)
        .filter(Track.recording_id.in_(pool_ids))
        .distinct()
        .all()
    }

    perf_by_rec = {r.id: (r.performance.performer_id if r.performance else None) for r in pool}
    genre_by_performer = _genre_by_performer(
        {pid for pid in perf_by_rec.values() if pid is not None}
    )

    # Stable within a day; `reroll` (an incrementing counter kept client-side,
    # not persisted) is the escape hatch for "Show me three more".
    seed = f"{_date.today().isoformat()}:{reroll}"
    rnd = random.Random(seed)
    unplayed = [r for r in pool if r.id not in played_ids]
    played   = [r for r in pool if r.id in played_ids]
    rnd.shuffle(unplayed)
    rnd.shuffle(played)
    ordered = unplayed + played   # unplayed strongly preferred, never excluded

    picks = _select_diverse(ordered, limit, perf_by_rec, genre_by_performer)
    # No waveform as of 2026-08-07: the Browse card is a handbill rendered from
    # metadata, so shipping downsampled peaks here was pure payload. The
    # serializer's opt-in param is untouched and still tested.
    #
    # card=True unconditionally — unlike /recent, this endpoint has exactly one
    # consumer (the Recommended module), so there is no flat-list caller to
    # protect and no reason to make it a parameter.
    return jsonify([recording_row(r, card=True) for r in picks])


# ── GET /api/recordings/on-this-day ───────────────────────────────────────────
# Browse view's "On This Day" module. Recordings whose performance date
# matches today's month/day, any year — no schema change, just a date-part
# match. Compact list, not cards, so no waveform. Hidden client-side when
# empty, which is most days (see the design spec's "every module hides
# entirely when empty" rule).

@bp.route("/on-this-day")
@login_required
def on_this_day():
    # The client passes its OWN month/day. Using the server's UTC date was
    # wrong for anyone west of Greenwich: after 17:00 Pacific, UTC is already
    # tomorrow, so the module showed tomorrow's shows (Ryan, 2026-08-23 —
    # "it's off by a day according to my local clock"). There is no correct
    # server-side answer, because "today" is a property of where the reader is
    # standing. UTC stays as the fallback for a caller that says nothing.
    month = request.args.get("month", type=int)
    day   = request.args.get("day", type=int)
    if not (month and day and 1 <= month <= 12 and 1 <= day <= 31):
        today = datetime.now(timezone.utc).date()
        month, day = today.month, today.day
    recs = (
        Recording.query
        .join(Performance, Recording.performance_id == Performance.id)
        .filter(Performance.start_month == month,
                Performance.start_day == day)
        .order_by(Performance.start_year.asc().nullslast())
        .all()
    )
    return jsonify([recording_row(r) for r in recs])


# ── GET /api/recordings/<id> ──────────────────────────────────────────────────

@bp.route("/<int:recording_id>")
@login_required
def get_recording(recording_id):
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    def _analysis(ta):
        """Serialise a TrackAnalysis row, or return None if not yet run."""
        if ta is None:
            return None
        return {
            "sample_rate_hz":       ta.sample_rate_hz,
            "bit_depth":            ta.bit_depth,
            "bitrate_kbps":         ta.bitrate_kbps,
            "rms_db":               ta.rms_db,
            "peak_db":              ta.peak_db,
            "noise_floor_db":       ta.noise_floor_db,
            "dynamic_range_db":     ta.dynamic_range_db,
            "clipping_pct":         ta.clipping_pct,
            "dc_offset":            ta.dc_offset,
            "spectral_centroid_hz": ta.spectral_centroid_hz,
            "spectral_cutoff_hz":   ta.spectral_cutoff_hz,
            "bpm":                  ta.bpm,
            "waveform":             _json.loads(ta.waveform_json) if ta.waveform_json else [],
            "analyzed_at":          ta.analyzed_at.isoformat() if ta.analyzed_at else None,
        }

    return jsonify({
        "id":                   rec.id,
        "performance_id":       rec.performance_id,
        "title":                rec.title,
        "source":               rec.source,
        "lineage":              rec.lineage,
        "quality":              rec.quality,
        "is_favorite":          bool(rec.is_favorite),
        "is_complete":          rec.is_complete,
        "is_official":          bool(rec.is_official),
        # Exposed so the page can SAY the show is out at the workbench. A
        # recording whose folder has been moved would otherwise look completely
        # normal and simply fail to play — the "empty state that is really a
        # failed fetch" trap, in its most confusing form.
        "is_published":         bool(rec.is_published),
        "info_file_content":    rec.info_file_content,
        "notes":                rec.notes,
        "ai_research":          _json.loads(rec.ai_research_json) if rec.ai_research_json else None,
        "collections": [
            {"id": l.collection.id, "name": l.collection.name}
            for l in db.session.query(CollectionRecording).filter_by(recording_id=rec.id).all()
        ],
        "tracks": [
            {
                "id":           t.id,
                "track_number": t.track_number,
                "title":        t.title,
                "set_number":   t.set_number,
                "duration":     t.duration,
                "is_official":  bool(t.is_official),
                "flags":        _json.loads(t.flags) if t.flags else [],
                "songwriter":   t.songwriter,
                "notes":        t.notes,
                "stream_url":   f"/api/stream/{t.id}",
                "analysis":     _analysis(t.analysis),
                "checksum": {
                    "type":            t.checksum_type,
                    "expected":        t.expected_checksum,
                    "status":          t.checksum_status,
                    "verified_at":     t.checksum_verified_at.isoformat() if t.checksum_verified_at else None,
                } if t.checksum_type else None,
            }
            for t in rec.tracks
        ],
        "fingerprints": [
            {
                "type":     fp.fingerprint_type,
                "filename": fp.filename,
            }
            for fp in rec.fingerprints
        ],
        "events": [
            {
                "event_type": e.event_type,
                "note":       e.note,
                "created_at": e.created_at.isoformat(),
                "user_id":    e.user_id,
            }
            for e in rec.events
        ],
    })


# ── GET /api/recordings/favorites ─────────────────────────────────────────────

@bp.route("/favorites")
@login_required
def favorite_recordings():
    """
    GET /api/recordings/favorites[?limit=N]

    Every starred recording, for the sidebar's Favorites list (2026-08-22; no
    longer a collapsible section as of the 2026-08-23 Left Nav Refinement, but
    still its own endpoint for the same reason). Its own endpoint rather than
    a `?favorite=1` flag on /recent, because the two answer different
    questions and want different orders: /recent is "what arrived lately" and
    is inherently capped, this is "the shelf I keep coming back to" and wants
    to be complete.

    Ordered by performer then date — a Favorites list is browsed by looking for
    a name, not by when the star happened to be clicked. `is_favorite` carries
    no timestamp anyway, which is deliberate: it is a one-click reaction, not an
    event log (see Recording.is_favorite).

    The limit is a runaway guard, not a feature. 200 favourites in a sidebar is
    already unusable and would want its own page.

    card=True unconditionally, added 2026-08-23 — the sidebar row now shows a
    small performer thumbnail beside the show's full title, which needs
    `image_id`. Eager-loaded via `_card_eager` for the same N+1 reason every
    other card=True caller uses it (see that function's docstring); a
    favorites list can realistically run to the same size as /recent's card
    rows, so the same care applies here.
    """
    limit = request.args.get("limit", 200, type=int) or 200
    limit = max(1, min(limit, 500))
    # coalesce(sort_name, name), not sort_name: the column is NULL for every one
    # of the 179 performers in the library (checked 2026-08-22 —
    # scripts/backfill_sort_names.py has never been run against it). Ordering on
    # it alone would tie every row and fall back to whatever order SQLite felt
    # like. This expression is the right one regardless of whether that backfill
    # ever happens.
    sort_key = db.func.coalesce(Performer.sort_name, Performer.name)
    recs = _card_eager(
        Recording.query
        .filter(Recording.is_favorite.is_(True))
        .join(Performance, Recording.performance_id == Performance.id)
        .outerjoin(Performer, Performance.performer_id == Performer.id)
        .order_by(sort_key.asc(),
                  Performance.start_year.asc(),
                  Performance.start_month.asc(),
                  Performance.start_day.asc())
    ).limit(limit).all()
    return jsonify([recording_row(r, card=True) for r in recs])


# ── POST /api/recordings/scan ─────────────────────────────────────────────────

@bp.route("/scan", methods=["POST"])
@login_required
def scan_recording():
    """
    Step 1 of ingest — non-destructive scan of a source folder.
    Returns two parallel metadata sets (from_tags, from_info_file) for
    the user to review field by field in the UI. Nothing is written to DB.
    Delegates to build_scan_payload() — the same function batch import uses,
    so a folder's health score never differs between the two flows.

    Also runs Paula (app.utils.paula.compute_paula_score) — the free,
    non-AI completeness/confidence scorer, 2026-07-16. Paula needs real DB
    data to fuzzy-match tag/txt-inferred Performer and Venue names, which is
    why she runs here (DB access) rather than inside build_scan_payload
    (kept DB-free/pure, same as compute_health()). Scoped to the interactive
    Add Recording flow only for now — batch-scan is untouched.
    """
    from app.utils.debug_log import log_step

    data        = request.get_json()
    folder_path = data.get("folder_path", "").strip()
    job         = f"scan:{folder_path}"

    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({"error": "Invalid or inaccessible folder path"}), 400

    log_step(job, "request received", "POST /api/recordings/scan")
    resp = build_scan_payload(folder_path)
    if resp is None:
        return jsonify({"error": "No audio files found in folder"}), 422

    known_performers = [p.name for p in Performer.query.all()]
    known_venues = [
        {"name": v.name, "city": v.city, "state": v.state, "country": v.country}
        for v in Venue.query.all()
    ]
    log_step(job, "queried known performers/venues",
             f"{len(known_performers)} performers, {len(known_venues)} venues")
    resp["paula"] = compute_paula_score(resp, known_performers, known_venues)
    log_step(job, "response ready", "Paula scoring done")

    return jsonify(resp)


# ── PUT /api/recordings/<id> ──────────────────────────────────────────────────

@bp.route("/<int:recording_id>", methods=["PUT"])
@login_required
def update_recording(recording_id):
    """
    Update recording metadata in DB.
    Logs a metadata_updated event.
    Does NOT write FLAC tags — that is a separate deliberate action.
    """
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    # TODO: validate archivist permission for this recording's artist

    data = request.get_json()
    # "rating" deliberately absent since 2026-08-18 — the field is retired from
    # the product, so accepting a write for it would quietly repopulate a column
    # nothing reads. See app/utils/serialize.py for the rationale.
    updatable = ["title", "source", "lineage",
                 "quality", "is_complete", "notes", "info_file_content"]
    for field in updatable:
        if field in data:
            setattr(rec, field, data[field])

    # is_official — cascade True to all tracks; never force-cascade False
    if "is_official" in data:
        rec.is_official = bool(data["is_official"])
        if rec.is_official:
            for t in rec.tracks:
                t.is_official = True

    # is_favorite — coerced rather than assigned straight through, because the
    # column is NOT NULL and the star toggle is the one field likely to be sent
    # from a checkbox as "" or null.
    if "is_favorite" in data:
        rec.is_favorite = bool(data["is_favorite"])

    # Log the change
    event = RecordingEvent(
        recording_id = rec.id,
        user_id      = current_user.id,
        event_type   = "metadata_updated",
        note         = data.get("change_note"),
    )
    db.session.add(event)

    # Folder name follows the metadata (decided 2026-07-25, built 2026-08-09)
    # — only Source lives on Recording and feeds the folder name, but this
    # runs on every save rather than gating on "source" in data: it's a cheap
    # no-op comparison when nothing folder-relevant changed, and staying
    # unconditional means one fewer place this can drift out of sync with
    # build_folder_name() later growing a new input field. Non-fatal: a
    # rename failure never blocks the metadata commit below.
    library_root  = current_app.config.get("LIBRARY_ROOT", "")
    rename_error  = rename_recording_folder(rec, library_root)

    db.session.commit()

    resp = {"id": rec.id, "updated_at": rec.updated_at.isoformat()}
    if rename_error:
        resp["folder_rename_error"] = rename_error
    return jsonify(resp)


# ── DELETE /api/recordings/<id> ──────────────────────────────────────────────

def _delete_tracks_of_recording(recording_id):
    """
    Delete every track of a recording along with its dependent child rows
    (track_analysis, play_log). Done in app code because SQLite FK cascades
    are not enforced on the existing schema — see delete_recording.
    """
    from app.models.track import Track
    from app.models.track_analysis import TrackAnalysis
    from app.models.play_log import PlayLog

    track_ids = [
        t.id for t in db.session.query(Track.id).filter_by(recording_id=recording_id).all()
    ]
    if track_ids:
        db.session.query(TrackAnalysis).filter(TrackAnalysis.track_id.in_(track_ids)).delete(
            synchronize_session=False)
        db.session.query(PlayLog).filter(PlayLog.track_id.in_(track_ids)).delete(
            synchronize_session=False)
        db.session.query(Track).filter(Track.id.in_(track_ids)).delete(
            synchronize_session=False)


@bp.route("/<int:recording_id>", methods=["DELETE"])
@login_required
def delete_recording(recording_id):
    """
    Delete a recording and all its child records (tracks, events, fingerprints).
    Then prune any performance/performer/canonical-artist left empty by the
    delete.

    `?delete_files=1` ALSO removes the recording's folder from disk (Ryan,
    2026-08-21 — offered as an unchecked option in the confirm dialog). Default
    remains files-untouched: for a ROIO collector the tape is the irreplaceable
    thing and the database row is not, so destroying audio is never the default
    and never implicit.

    The guard is deliberately paranoid, in the same spirit as
    /api/quality/move. The folder is resolved server-side from LIBRARY_ROOT +
    the row's relative folder_path — the client cannot name a path — and then
    it must still realpath INSIDE LIBRARY_ROOT, be a directory, and not be a
    mount point or a filesystem root. A folder that fails any of those is left
    alone and reported back; the row is still deleted, because "I could not
    remove the audio" is not a reason to keep a library entry the user asked
    to be rid of.

    Files go first, on purpose. If rmtree fails the row survives, the response
    says why, and the recording is still there to try again — whereas deleting
    the row first would strip the only handle on the folder the moment the
    filesystem misbehaved.
    """
    import shutil
    from app.models.recording import RecordingFingerprint
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Recording not found"}), 404

    performance_id = rec.performance_id

    delete_files = request.args.get("delete_files") in ("1", "true", "yes")
    files_deleted, files_error = False, None
    if delete_files:
        library_root = os.path.realpath(str(current_app.config.get("LIBRARY_ROOT", "")))
        target = os.path.realpath(os.path.join(library_root, rec.folder_path or ""))
        if not library_root or not rec.folder_path:
            files_error = "No library folder recorded for this recording"
        elif not (target != library_root and target.startswith(library_root + os.sep)):
            files_error = "Recording folder does not resolve inside the library"
        elif not os.path.isdir(target):
            files_error = "Recording folder is not on disk"
        elif os.path.ismount(target) or os.path.dirname(target) == target:
            files_error = "Refusing to delete a mount point or filesystem root"
        else:
            try:
                shutil.rmtree(target)
                files_deleted = True
            except OSError as e:
                files_error = "Could not delete folder: %s" % e

    # Delete children explicitly, bottom-up. SQLite FK enforcement is off and
    # the existing tables were created without ON DELETE actions, so nothing
    # cascades for us — we clear every child of every track (analysis, play
    # logs) plus the recording's own children, or they orphan silently. Bulk
    # deletes (no ORM cascade) keep this predictable.
    _delete_tracks_of_recording(recording_id)
    db.session.query(RecordingFingerprint).filter_by(recording_id=recording_id).delete(
        synchronize_session=False)
    db.session.query(RecordingEvent).filter_by(recording_id=recording_id).delete(
        synchronize_session=False)
    db.session.query(Recording).filter_by(id=recording_id).delete(synchronize_session=False)
    db.session.flush()

    # Prune the now-empty chain above the recording.
    pruned = prune_after_recording_delete(performance_id)

    db.session.commit()

    return jsonify({"deleted": recording_id, "pruned": pruned,
                    "files_deleted": files_deleted, "files_error": files_error}), 200


# ── POST /api/recordings/<id>/info-file ──────────────────────────────────────

@bp.route("/<int:recording_id>/info-file", methods=["POST"])
@login_required
def save_recording_info_file(recording_id):
    """
    POST /api/recordings/<id>/info-file
    Body: { content }
    Returns: { ok, wrote_file, filename|null, reason|null }

    Persist edited info-file text to the recording row and, when the library
    folder already holds a text file, rewrite that file in place.

    It deliberately does NOT create a file when the folder has none (Ryan,
    2026-08-21). The database is the source of truth and a write into a
    collector's library is always an explicit act; inventing a filename for a
    folder that never carried an info file would be Flux authoring content in
    someone else's archive. In that case the DB save still happens and the
    caller gets wrote_file=False with a reason the pane can show.

    The target file is picked with the same scoring scan_folder() uses, and
    checksum lists are excluded by the same content sniff — so the file the
    pane was populated from is the file that gets rewritten, rather than an
    .md5 list that happens to sort first. (See the 2026-08-02 trap in
    CONTEXT.md: a checksum file named after the show once won info-file
    scoring outright.)
    """
    from app.utils.ingest import _score_text_file, fingerprint_type_for_file

    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    content = (request.get_json() or {}).get("content", "")
    if not isinstance(content, str):
        return jsonify({"error": "content must be a string"}), 400

    # DB first — it is the source of truth, and it must land even if the disk
    # write is impossible (unmounted volume, read-only media, missing folder).
    rec.info_file_content = content or None
    db.session.add(RecordingEvent(
        recording_id = rec.id,
        user_id      = current_user.id,
        event_type   = "metadata_updated",
        note         = "Edited info file",
    ))
    db.session.commit()

    library_root = current_app.config.get("LIBRARY_ROOT", "")
    folder = os.path.join(library_root, rec.folder_path or "")
    if not rec.folder_path or not os.path.isdir(folder):
        return jsonify({"ok": True, "wrote_file": False, "filename": None,
                        "reason": "Recording folder is not reachable"})

    # Candidate = a .txt that is not a checksum list, best-scoring first.
    candidates = []
    for fname in os.listdir(folder):
        full = os.path.join(folder, fname)
        low  = fname.lower()
        if not low.endswith(".txt") or not os.path.isfile(full):
            continue
        if fingerprint_type_for_file(full, low):
            continue
        candidates.append((_score_text_file(fname), fname))
    if not candidates:
        return jsonify({"ok": True, "wrote_file": False, "filename": None,
                        "reason": "No info file in the recording folder"})

    target = sorted(candidates, key=lambda c: (-c[0], c[1]))[0][1]
    try:
        with open(os.path.join(folder, target), "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return jsonify({"ok": True, "wrote_file": False, "filename": target,
                        "reason": "Could not write file: %s" % e})

    return jsonify({"ok": True, "wrote_file": True, "filename": target, "reason": None})


# ── POST /api/recordings/<id>/move ───────────────────────────────────────────

@bp.route("/<int:recording_id>/move", methods=["POST"])
@login_required
def move_recording_out(recording_id):
    """
    POST /api/recordings/<id>/move
    Body: { destination: "workshop" | "backlog" }
    Returns: { ok, destination, moved_to_name }

    Takes an ingested recording back off the shelf: physically moves its folder
    out of the library into one of the TRIAGE_DIRS and sets is_published=False.
    The library record survives in full — metadata, lineage, checksums, event
    history — so a show that comes back does not have to be re-ingested.

    Deliberately its OWN endpoint rather than reusing POST /api/quality/move
    (Ryan, 2026-08-21). That one takes an absolute folder path from the client,
    which is fine for triage — the client is looking at a scan of a folder it
    named — but it is exactly what the library's path obfuscation forbids: the
    frontend knows recording IDs and nothing else, and Flask resolves paths.
    Here the id is the input and the path is derived server-side.

    Same paranoia as the delete-files path, for the same reason: this moves
    user files. The source must resolve INSIDE LIBRARY_ROOT, be a directory,
    and not be a mount point or root; the destination must be a configured
    TRIAGE_DIR. Never overwrites — a name collision gets " (2)", " (3)", …
    rather than merging two shows, matching /api/quality/move's behaviour.

    Files move BEFORE the flag flips. A failed move leaves a published
    recording whose folder is exactly where it always was, which is the state
    the user can see and retry from; flipping the flag first would hide a show
    that never actually went anywhere.
    """
    import shutil

    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    destination = ((request.get_json() or {}).get("destination") or "").strip().lower()
    triage_dirs = current_app.config.get("TRIAGE_DIRS", {})
    if destination not in triage_dirs:
        return jsonify({"error": f"Unknown destination {destination!r}; "
                                 f"expected one of {sorted(triage_dirs)}"}), 400

    if not rec.is_published:
        return jsonify({"error": "This recording is already out of the library"}), 409

    library_root = os.path.realpath(str(current_app.config.get("LIBRARY_ROOT", "")))
    src = os.path.realpath(os.path.join(library_root, rec.folder_path or ""))
    if not library_root or not rec.folder_path:
        return jsonify({"error": "No library folder recorded for this recording"}), 400
    if not (src != library_root and src.startswith(library_root + os.sep)):
        return jsonify({"error": "Recording folder does not resolve inside the library"}), 403
    if not os.path.isdir(src):
        return jsonify({"error": "Recording folder is not on disk"}), 400
    if os.path.ismount(src) or os.path.dirname(src) == src:
        return jsonify({"error": "Refusing to move a mount point or filesystem root"}), 400

    dest_root = triage_dirs[destination]
    try:
        os.makedirs(dest_root, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"Cannot create {dest_root}: {e}"}), 500

    base, target, n = os.path.basename(src), None, 2
    target = os.path.join(dest_root, base)
    while os.path.exists(target):
        target = os.path.join(dest_root, f"{base} ({n})")
        n += 1

    try:
        shutil.move(src, target)
    except OSError as e:
        return jsonify({"error": f"Move failed: {e}"}), 500

    rec.is_published = False
    # folder_path is left alone on purpose — see the model comment. The event is
    # the durable record of where the folder actually went, and it is the only
    # one: nothing else on the row can express "it is in Workshop now".
    db.session.add(RecordingEvent(
        recording_id = rec.id,
        user_id      = current_user.id,
        event_type   = "moved_out_of_library",
        note         = f"Moved to {destination} as {os.path.basename(target)}",
    ))
    db.session.commit()

    return jsonify({"ok": True, "destination": destination,
                    "moved_to_name": os.path.basename(target)})


# ── POST /api/recordings/<id>/write-tags ──────────────────────────────────────

@bp.route("/<int:recording_id>/write-tags", methods=["POST"])
@login_required
def write_tags(recording_id):
    """
    Write current DB metadata as Vorbis comments to every FLAC file in the
    recording. Existing tags are replaced entirely.

    This is a deliberate, explicit action — not triggered by metadata saves.
    Logs a tags_written event on full or partial success.

    Returns:
      200  { written: n, errors: [(filename, msg), ...] }
      404  if recording not found
      500  if all files failed
    """
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    library_root = current_app.config.get("LIBRARY_ROOT", "")
    n_written, errors = write_flac_tags(rec, library_root)

    # Log even on partial success so the event trail is accurate
    if n_written > 0:
        note = f"{n_written} file(s) written"
        if errors:
            note += f"; {len(errors)} error(s): " + "; ".join(f[0] for f in errors)
        event = RecordingEvent(
            recording_id = rec.id,
            user_id      = current_user.id,
            event_type   = "tags_written",
            note         = note,
        )
        db.session.add(event)
        db.session.commit()

    if n_written == 0:
        return jsonify({"error": "No files written", "errors": errors}), 500

    return jsonify({"written": n_written, "errors": errors})


# ── POST /api/recordings/<id>/reveal ─────────────────────────────────────────

@bp.route("/<int:recording_id>/reveal", methods=["POST"])
@login_required
def reveal_folder(recording_id):
    """
    Open this recording's folder in Finder.

    The path itself never reaches the frontend — same File obfuscation rule
    as everything else in this module — so this resolves it server-side and
    shells out to `open`. Only makes sense on the single Mac this app already
    runs on (PyWebView, one machine, one library root); nothing here is
    reachable through the peer door.
    """
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    library_root = current_app.config.get("LIBRARY_ROOT", "")
    folder_abs   = os.path.join(str(library_root), rec.folder_path)

    if not os.path.isdir(folder_abs):
        return jsonify({"error": "This recording's folder no longer exists on disk."}), 404

    try:
        subprocess.Popen(["open", folder_abs])
    except OSError as e:
        return jsonify({"error": f"Could not open Finder: {e}"}), 500

    return jsonify({"opened": True})


# ── GET /api/recordings/<id>/tags ─────────────────────────────────────────────

@bp.route("/<int:recording_id>/tags")
@login_required
def get_recording_file_tags(recording_id):
    """
    Return the actual on-disk Vorbis comments for every FLAC file in the
    recording. Powers the "File Tags" viewer so the effect of "Write Tags to
    Files" is visible. File paths are never exposed.
    """
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    library_root = current_app.config.get("LIBRARY_ROOT", "")
    return jsonify({
        "recording_id": rec.id,
        "tracks":       read_recording_tags(rec, library_root),
    })


# ── POST /api/recordings/<id>/reprocess ───────────────────────────────────────

@bp.route("/<int:recording_id>/reprocess", methods=["POST"])
@login_required
def reprocess_recording(recording_id):
    """
    Re-run Librosa analysis on every track in the recording.
    Results are upserted into track_analysis. Safe to call multiple times.

    Returns:
      200  { analysed: n, errors: [(filename, msg), ...] }
      404  if recording not found
      500  if librosa is unavailable or all tracks failed
    """
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    library_root = current_app.config.get("LIBRARY_ROOT", "")
    n_ok, errors = analyse_recording(rec, library_root, db.session)

    # Log the reprocess event
    db.session.add(RecordingEvent(
        recording_id = rec.id,
        user_id      = current_user.id,
        event_type   = "reprocessed",
        note         = f"{n_ok} track(s) analysed" + (
            f"; {len(errors)} error(s)" if errors else ""
        ),
    ))
    db.session.commit()

    if n_ok == 0:
        return jsonify({"error": "Analysis failed for all tracks", "errors": errors}), 500

    return jsonify({"analysed": n_ok, "errors": errors})


# ── POST /api/recordings/<id>/verify-checksums ───────────────────────────────

@bp.route("/<int:recording_id>/verify-checksums", methods=["POST"])
@login_required
def verify_checksums(recording_id):
    """
    (Re-)validate this recording's fingerprint checksums against the audio
    files currently sitting in the library. Safe to call any time — nothing
    here depends on the original source folder still existing.

    Also opportunistically discovers fingerprint files that were copied into
    the library with this recording but never parsed into RecordingFingerprint
    rows (covers shows ingested before this feature existed) — so this one
    endpoint serves both "re-validate" and "go back and process the ones I
    already have in the library."
    """
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    library_root = current_app.config.get("LIBRARY_ROOT", "")
    folder_abs   = os.path.join(str(library_root), rec.folder_path)

    # Collect into a local list rather than re-reading rec.fingerprints after
    # adding to it — the relationship collection was already cached by the
    # line above and db.session.flush() doesn't invalidate that cache, so a
    # freshly-discovered row wouldn't show up in it this same request.
    all_fingerprints = list(rec.fingerprints)
    known_filenames  = {fp.filename for fp in all_fingerprints}
    for found in discover_fingerprint_files(folder_abs):
        if found["filename"] in known_filenames:
            continue
        try:
            with open(os.path.join(folder_abs, found["rel_path"]),
                      "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            content = None
        new_fp = RecordingFingerprint(
            recording_id     = rec.id,
            fingerprint_type = found["type"],
            filename         = found["filename"],
            content          = content,
        )
        db.session.add(new_fp)
        all_fingerprints.append(new_fp)

    # Same FINGERPRINT_TYPE_PRIORITY tie-break as ingest (see
    # api/ingest.py _do_confirm) when more than one fingerprint file
    # is present: ffp, then md5, then st5.
    fingerprints = sorted(all_fingerprints,
                          key=lambda fp: FINGERPRINT_TYPE_PRIORITY.get(fp.fingerprint_type, 9))
    now = datetime.now(timezone.utc)
    checked = 0
    for fp in fingerprints:
        if not fp.content:
            continue
        matches = match_entries_to_tracks(parse_checksum_file(fp.content), rec.tracks)
        for track, expected in matches.items():
            abs_path = os.path.join(folder_abs, track.file_path)
            track.checksum_type        = fp.fingerprint_type
            track.expected_checksum    = expected
            track.checksum_status      = verify_track_checksum(abs_path, fp.fingerprint_type, expected)
            track.checksum_verified_at = now
            checked += 1
    db.session.commit()

    return jsonify({
        "verified_at": now.isoformat(),
        "checked":     checked,
        "tracks": [
            {"id": t.id, "checksum_type": t.checksum_type,
             "checksum_status": t.checksum_status, "expected_checksum": t.expected_checksum}
            for t in rec.tracks
        ],
    })
