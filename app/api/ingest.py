"""
api/ingest.py — Full ingest confirmation endpoint.

POST /api/ingest/confirm

Handles the full "resolve or create" chain from a single payload:
  CanonicalArtist → Artist → Venue (optional) → Performance → Recording + Tracks

This avoids the frontend needing to pre-resolve IDs. The user just
provides names and dates; this endpoint does the lookup/create work.
"""

import re
import os
import json
import queue
import threading
import time
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user
from sqlalchemy import func

_AUDIO_EXTS = {'.flac', '.mp3', '.wav', '.aiff', '.aif', '.m4a', '.ogg', '.ape', '.wv'}

from app.extensions import db
from app.models.performer import Performer
from app.models.artist import Artist, Membership
from app.utils.performers import resolve_or_create_performer
from app.utils.personnel import sync_performance_personnel
from app.utils.venues import is_placeholder_venue_name
from app.models.venue import Venue
from app.models.event import Event
from app.models.performance import Performance
from app.models.recording import Recording, RecordingFingerprint
from app.models.recording_event import RecordingEvent
from app.models.track import Track
from app.models.user_preference import UserPreference
from app.utils.ingest import (move_to_library, compute_audio_rename_map,
                              IngestCancelled)
from app.utils.folder_naming import build_folder_name
from app.utils.ai_assist import run_ai_assist, AiAssistError
from app.utils.prefs import get_api_key, get_pref
from app.utils.health import compute_health
from app.utils.checksums import (
    parse_checksum_file, match_entries_to_tracks, verify_track_checksum,
    FINGERPRINT_TYPE_PRIORITY,
)
from app.utils.debug_log import log_step

bp = Blueprint("ingest", __name__)


class _ChecksumMatchProxy:
    """
    Stand-in for a Track during fingerprint matching (step 9 of _do_confirm).

    match_entries_to_tracks() matches by reading `.file_path` / `.track_number`
    off whatever it's given. Since Track.file_path now always holds the NEW
    flattened+renamed filename (see compute_audio_rename_map / move_to_library
    in app.utils.ingest), matching a fingerprint file — which lists ORIGINAL
    filenames — directly against real Track rows would silently fail for any
    recording whose audio got renamed on ingest. This proxy presents the
    original filename to the matcher while `.real` routes a successful match
    back to the actual Track so the checksum lands on the right row.
    """
    def __init__(self, real_track, original_filename):
        self.real          = real_track
        self.file_path     = original_filename
        self.track_number  = real_track.track_number


@bp.route("/health", methods=["POST"])
@login_required
def health():
    """
    POST /api/ingest/health
    Thin wrapper over compute_health(scan). The client sends a scan-shaped
    payload (original for the initial score, or proposal-overlaid for the
    projected post-AI score) and gets back {score, band, factors}.
    """
    return jsonify(compute_health(request.get_json() or {}))


# In-memory AI-research jobs. The research call is far too slow (30-90s) to hold
# a synchronous HTTP request open — the webview aborts the fetch at ~60s. So we run
# it in a background thread and let the client poll for the result.
_AI_JOBS = {}  # job_id -> {"status": running|done|error, "result"/"error", "t0"}


def _run_ai_job(job_id, folder_path, current, api_key, model, *, recording_id=None, app=None):
    import time as _time
    import traceback as _tb
    t0 = _time.time()
    try:
        result = run_ai_assist(folder_path, current, api_key, model)
        _AI_JOBS[job_id] = {"status": "done", "result": result}
        print("[ai-assist] job %s ok in %.1fs" % (job_id[:8], _time.time() - t0), flush=True)
        # Persist to the recording, if this run was for an already-saved one.
        # Best-effort: a save failure shouldn't hide a successful research result
        # from the client, which already has it in _AI_JOBS.
        if recording_id and app is not None:
            try:
                with app.app_context():
                    from app.models.recording import Recording
                    rec = db.session.get(Recording, recording_id)
                    if rec:
                        rec.ai_research_json = json.dumps(result)
                        db.session.commit()
            except Exception:
                _tb.print_exc()
    except AiAssistError as e:
        _AI_JOBS[job_id] = {"status": "error", "error": str(e)}
        print("[ai-assist] job %s failed after %.1fs: %s" % (job_id[:8], _time.time() - t0, e), flush=True)
    except Exception as e:  # noqa: BLE001
        _tb.print_exc()
        _AI_JOBS[job_id] = {"status": "error", "error": "Unexpected error: %s" % e}


# How close two act names must read before they count as the same act. 0.85 is
# the threshold parse_info_file() already uses for fuzzy artist/venue matching
# — deliberately the same number, so the ingest form and the duplicate check
# never disagree about whether two names are "the same".
_PERFORMER_SIMILARITY = 0.85

# Words that decorate an act name without changing who it is. "Aoife O'Donovan"
# and "Aoife O'Donovan Band" are one act to a collector and two rows in the DB,
# which is exactly the variant Ryan asked this to catch (2026-08-02).
_ACT_NOISE_WORDS = {"band", "trio", "quartet", "quintet", "group", "ensemble",
                    "orchestra", "the", "and", "with", "featuring", "feat"}


def _act_key(name):
    """Normalise an act name down to the words that identify it."""
    words = re.sub(r"[^\w\s]", " ", (name or "").lower()).split()
    core  = [w for w in words if w not in _ACT_NOISE_WORDS]
    return " ".join(core or words)


def resolve_similar_performer_ids(artist_name):
    """
    Every Performer that plausibly IS the act named `artist_name`.

    Three widening passes, because a duplicate that goes unnoticed costs an
    accidental re-ingest and a duplicate flagged wrongly costs one glance:

      1. exact, case-insensitive — what resolve_or_create_performer() does, so
         this can never disagree with what Confirm would actually resolve to
      2. same normalised core after stripping act-noise words, so
         "Aoife O'Donovan" finds "Aoife O'Donovan Band"
      3. difflib ratio >= 0.85 on the core, catching spelling and punctuation
         drift ("Bela Fleck" / "Béla Fleck")

    Then the ARTIST (person) side: if a person of that name exists, every
    Performer they are a member of counts too — Ryan asked for "performer or
    artist, or a similar variant of either", and the 07-11 remodel makes those
    genuinely different tables.
    """
    from difflib import SequenceMatcher

    name = (artist_name or "").strip()
    if not name:
        return []

    ids  = set()
    key  = _act_key(name)
    rows = db.session.query(Performer.id, Performer.name).all()

    for pid, pname in rows:
        if pname and pname.lower() == name.lower():
            ids.add(pid)
            continue
        pkey = _act_key(pname)
        if not pkey or not key:
            continue
        if pkey == key or SequenceMatcher(None, pkey, key).ratio() >= _PERFORMER_SIMILARITY:
            ids.add(pid)

    person = db.session.query(Artist).filter(
        func.lower(Artist.name) == name.lower()).first()
    if person:
        ids.update(m.performer_id for m in person.memberships)

    return sorted(ids)


@bp.route("/check-existing", methods=["GET"])
@login_required
def check_existing():
    """
    GET /api/ingest/check-existing?artist_name=...&year=...&month=...&day=...
    Read-only lookup (no creation) — the Add Recording form calls this once
    performer + date are known, to WARN (not block) when the library already
    has a performance for that performer/date. Multiple recordings per
    performance are legitimate (SBD + AUD of the same show), so this never
    prevents Confirm — it just surfaces what's already there so an archivist
    doesn't accidentally re-ingest a tape they already have. Ryan, 2026-07-14.

    Matches on performer name the same way resolve_or_create_performer does
    (case-insensitive exact match) so this never disagrees with what Confirm
    would actually resolve to. Month/day narrow the match; year is required —
    without it there's nothing meaningful to match on.
    """
    from app.utils.format import format_partial_date

    artist_name = (request.args.get("artist_name") or "").strip()
    year  = request.args.get("year",  type=int)
    month = request.args.get("month", type=int)
    day   = request.args.get("day",   type=int)
    if not artist_name or not year:
        return jsonify({"performer_found": False, "performances": []})

    performer_ids = resolve_similar_performer_ids(artist_name)
    if not performer_ids:
        return jsonify({"performer_found": False, "performances": []})

    q = db.session.query(Performance).filter(
        Performance.performer_id.in_(performer_ids),
        Performance.start_year == year,
    )
    if month:
        q = q.filter(Performance.start_month == month)
    if day:
        q = q.filter(Performance.start_day == day)

    performances = []
    for p in q.all():
        if not p.recordings:
            continue   # nothing recorded against it yet — no duplicate risk
        v = p.venue
        performances.append({
            "id":    p.id,
            "date":  format_partial_date(p.start_year, p.start_month, p.start_day),
            "venue": v.name if v else None,
            # Which act it actually matched, so a fuzzy/variant hit is never
            # mistaken for an exact one ("Aoife O'Donovan Band" vs "Aoife
            # O'Donovan" are different rows and the user must see which).
            "performer": p.performer.name if p.performer else None,
            "recordings": [
                {
                    "id":          r.id,
                    "source":      r.source,
                    "quality":     r.quality,
                    "track_count": len(r.tracks),
                    "created_at":  r.created_at.isoformat() if r.created_at else None,
                }
                for r in p.recordings
            ],
        })
    return jsonify({"performer_found": True, "performances": performances})


@bp.route("/save-info-file", methods=["POST"])
@login_required
def save_info_file():
    """
    POST /api/ingest/save-info-file
    Write edited info-file text back to disk in the (not-yet-ingested) scan
    folder, independent of Confirm — so an archivist can save corrections,
    then re-run AI Assist against the fixed-up file. When `filename` matches
    a text file scan_folder() already found in that folder, the target path
    is re-derived server-side from that scan (not trusted from the client).
    When it doesn't match anything scanned — the folder had no info file and
    one was typed in from scratch — a new file is created in folder_path,
    provided the name is a bare filename (no path separators/traversal).

    Body: { folder_path, filename, content }
    """
    from app.utils.ingest import scan_folder

    data        = request.get_json() or {}
    folder_path = (data.get("folder_path") or "").strip()
    filename    = (data.get("filename") or "").strip()
    content     = data.get("content", "")
    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({"error": "Invalid or inaccessible folder path"}), 400
    if not filename:
        return jsonify({"error": "No filename given"}), 400

    scan  = scan_folder(folder_path)
    match = next((tf for tf in scan["text_files"] if tf["filename"] == filename), None)
    if match:
        target_path = match["path"]
    else:
        safe_name = os.path.basename(filename)
        if not safe_name or safe_name != filename:
            return jsonify({"error": "Invalid filename"}), 400
        target_path = os.path.join(folder_path, safe_name)

    try:
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return jsonify({"error": "Could not write file: %s" % e}), 500

    return jsonify({"ok": True, "filename": os.path.basename(target_path)})


@bp.route("/ai-assist", methods=["POST"])
@login_required
def ai_assist():
    """
    POST /api/ingest/ai-assist
    Kick off an AI research job (background thread) and return a job id
    immediately. Poll GET /api/ingest/ai-assist/<job_id> for the result.

    Body: { folder_path, current: {...} }
    """
    import threading
    import uuid

    data        = request.get_json() or {}
    folder_path = (data.get("folder_path") or "").strip()
    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({"error": "Invalid or inaccessible folder path"}), 400

    api_key = get_api_key(current_user.id)
    if not api_key:
        # 428 → frontend routes the user to add their key in Settings.
        return jsonify({"error": "no_api_key"}), 428
    model = get_pref(current_user.id, "ai_model") or "claude-sonnet-5"

    job_id = uuid.uuid4().hex
    _AI_JOBS[job_id] = {"status": "running"}
    # Key/model are resolved here (request context); the thread needs no DB access.
    threading.Thread(
        target=_run_ai_job,
        args=(job_id, folder_path, data.get("current") or {}, api_key, model),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id}), 202


@bp.route("/ai-assist-recording/<int:recording_id>", methods=["POST"])
@login_required
def ai_assist_recording(recording_id):
    """Run AI research for an already-saved recording (background job). Builds the
    `current` metadata from the DB — run_ai_assist reads no files."""
    import threading
    import uuid
    from app.models.recording import Recording
    from app.utils.format import format_partial_date

    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    p = rec.performance
    v = p.venue if p else None
    current = {
        "artist":  (p.performer.name if (p and p.performer) else ""),
        "date":    format_partial_date(p.start_year, p.start_month, p.start_day) if p else "",
        "venue":   (v.name if v else ""),
        "city":    (v.city if v else (p.city if p else "")),
        "state":   (v.state if v else (p.state if p else "")),
        "country": (v.country if v else (p.country if p else "")),
        "source":  rec.source or "",
        "lineage": rec.lineage or "",
        "tracks":  [{"number": t.track_number, "title": t.title, "duration": t.duration}
                    for t in rec.tracks],
        "info_file_content": rec.info_file_content or "",
    }

    api_key = get_api_key(current_user.id)
    if not api_key:
        return jsonify({"error": "no_api_key"}), 428
    model = get_pref(current_user.id, "ai_model") or "claude-sonnet-5"

    job_id = uuid.uuid4().hex
    _AI_JOBS[job_id] = {"status": "running"}
    threading.Thread(
        target=_run_ai_job,
        args=(job_id, rec.folder_path or "", current, api_key, model),
        kwargs={"recording_id": recording_id, "app": current_app._get_current_object()},
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id}), 202


@bp.route("/ai-assist/<job_id>", methods=["GET"])
@login_required
def ai_assist_status(job_id):
    """Poll an AI research job. Returns running, or done+result / error (one-shot)."""
    job = _AI_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if job["status"] == "running":
        return jsonify({"status": "running"})
    _AI_JOBS.pop(job_id, None)  # deliver terminal state once, then discard
    if job["status"] == "error":
        return jsonify({"status": "error", "error": job["error"]})
    return jsonify({"status": "done", "result": job["result"]})


_INGEST_JOBS = {}  # job_id -> {status, phase, copied, total, result, error}

# ═════════════════════════════════════════════════════════════════════════════
# Confirm-job phases
# ═════════════════════════════════════════════════════════════════════════════
# The confirm job reported exactly two things — `copied` and `total` — which
# are only meaningful during ONE of its four phases. Everything before the copy
# (resolving the act, venue, event and performance) and everything after it
# (checksum verification, which reads every track when the folder ships .md5)
# showed as a motionless 0/0, so a slow ingest was indistinguishable from a
# hung one (Ryan, 2026-08-28: "help me and the user understand what may be
# taking a while").
#
# Labels are user-facing and deliberately name the WORK, not the code.
PHASES = {
    "resolving":  "Reading folder and matching metadata",
    "copying":    "Copying files into the library",
    "moving":     "Moving files into the library",
    "cataloging": "Cataloging tracks",
    "checksums":  "Verifying checksums",
    # Deliberately vague (Ryan, 2026-08-28: "don't be that specific"). The
    # phase measures each track to decide whether it looks like music, but
    # naming the heuristic in a progress line tells the user about our
    # internals rather than about their ingest.
    "signals":    "Analyzing tracks",
    "saving":     "Saving to the library",
    "queued":     "Queued for analysis",
    "analyzing":  "Analyzing audio for metrics",
    "done":       "Done",
}


# ═════════════════════════════════════════════════════════════════════════════
# Track-analysis queue
# ═════════════════════════════════════════════════════════════════════════════
# ONE worker, drained from a queue — not a thread per recording.
#
# The old line was `threading.Thread(target=_run_analysis_job, ...).start()`
# fired from inside each finished ingest, with nothing bounding it. Ingesting a
# folder of seventeen shows therefore spawned seventeen concurrent Librosa
# threads, each holding a fully decoded track in RAM (a 4.6-minute 24/96 track
# is 106 MB as float32) and each competing for the same CPU and the same NAS
# spindle as the copy still running for the NEXT show.
#
# Measured on one such track (2026-08-28): librosa.load 19.6 s, beat_track
# 29.1 s, full-track STFT + centroid 5.9 s — 57.7 s per track, roughly 16
# minutes for that 8-track show. Seventeen of those at once is the thing that
# actually made ingestion feel slow; the listening-quality pass everyone
# suspects costs about 3 seconds a show.
#
# A single worker does not make the analysis faster. It makes it FINITE, keeps
# it off the critical path of the next copy, and — because the queue is now a
# real object — makes it reportable, which is what the debug pane needs.
_ANALYSIS_Q = queue.Queue()
_ANALYSIS_STATE = {
    "current": None,      # {recording_id, name, started, tracks_done, tracks_total}
    "worker":  False,     # has the worker thread been started
    "done":    0,
    "failed":  0,
    "skipped": 0,         # recordings ingested in Quick Add, never enqueued
}


def _analysis_worker(app):
    """Drain the analysis queue forever, one recording at a time."""
    import traceback as _tb
    while True:
        recording_id = _ANALYSIS_Q.get()
        try:
            with app.app_context():
                from app.models.recording import Recording
                from app.utils.analysis import analyse_recording
                rec = db.session.get(Recording, recording_id)
                if not rec:
                    continue
                _ANALYSIS_STATE["current"] = {
                    "recording_id": recording_id,
                    "name": os.path.basename(rec.folder_path or "") or str(recording_id),
                    "started": time.time(),
                    "tracks_total": len(rec.tracks or []),
                }
                log_step(f"analysis:{recording_id}", "analyzing",
                         f"{len(rec.tracks or [])} tracks", force=True)
                library_root = app.config.get("LIBRARY_ROOT", "")
                n_ok, errors = analyse_recording(rec, library_root, db.session)
                _ANALYSIS_STATE["done"] += 1
                if errors:
                    _ANALYSIS_STATE["failed"] += len(errors)
                print("[ingest] auto-analysis for recording %s: %d ok, %d error(s)"
                      % (recording_id, n_ok, len(errors)), flush=True)
        except Exception:
            _ANALYSIS_STATE["failed"] += 1
            _tb.print_exc()
        finally:
            _ANALYSIS_STATE["current"] = None
            _ANALYSIS_Q.task_done()


def _enqueue_analysis(app, recording_id):
    """Hand a freshly ingested recording to the single analysis worker."""
    if not _ANALYSIS_STATE["worker"]:
        _ANALYSIS_STATE["worker"] = True
        threading.Thread(target=_analysis_worker, args=(app,), daemon=True).start()
    _ANALYSIS_Q.put(recording_id)


def analysis_snapshot():
    """Queue state for the debug pane. Never raises — it is a debug surface."""
    cur = _ANALYSIS_STATE.get("current")
    return {
        "pending": _ANALYSIS_Q.qsize(),
        "current": (dict(cur, elapsed=round(time.time() - cur["started"], 1))
                    if cur else None),
        "done":    _ANALYSIS_STATE["done"],
        "failed":  _ANALYSIS_STATE["failed"],
        "skipped": _ANALYSIS_STATE["skipped"],
    }


def _run_ingest_job(job_id, app, data, user_id):
    """Background worker: copy files (with progress) + create the DB chain."""
    import traceback as _tb
    from app.utils.ingest import IngestCancelled
    job = _INGEST_JOBS[job_id]
    try:
        with app.app_context():
            def prog(copied, total):
                job["copied"] = copied
                job["total"]  = total

            def phase(key, detail=None):
                job["phase"] = key
                job["phase_label"] = PHASES.get(key, key)
                job["phase_at"] = time.time()
                # Unconditional: a phase with no detail must CLEAR the previous
                # one, or "Saving to the library" inherits the checksum phase's
                # "(FFP, MD5)" and reports work it is not doing.
                job["phase_detail"] = detail
                log_step(f"ingest:{job_id}", key, detail or PHASES.get(key, key),
                         force=True)

            def cancelled():
                return bool(job.get("cancel"))

            try:
                job["result"] = _do_confirm(data, user_id, prog,
                                            cancel_cb=cancelled, phase_cb=phase)
            except IngestCancelled:
                # move_to_library has already put the filesystem back; the DB
                # session has not committed yet, so rolling back leaves no trace
                # of this recording. Anything ingested EARLIER in the queue was
                # committed by its own job and is deliberately untouched.
                db.session.rollback()
                job["status"] = "cancelled"
                return
            job["status"] = "done"
            job["phase"] = "done"
            job["phase_label"] = PHASES["done"]
        # Hand Librosa analysis to the single background worker once the
        # recording exists — decoupled from this job so it can't hold up
        # reporting "done".
        #
        # Quick Add (`skip_analysis`) simply does not enqueue. Nothing else
        # about the ingest changes: the same files are copied, the same tracks
        # and checksums are written, and the listening-quality score still
        # travels across in _do_confirm step 11. Only waveform/BPM/spectral are
        # deferred, and Re-Analyze fills them in later against the same rows —
        # which is exactly what makes this safe to skip (Ryan, 2026-08-28).
        rec_id = (job.get("result") or {}).get("recording_id")
        if rec_id:
            if data.get("skip_analysis"):
                _ANALYSIS_STATE["skipped"] += 1
                log_step(f"ingest:{job_id}", "analysis-skipped",
                         "Quick Add — waveform/BPM deferred to Re-Analyze",
                         force=True)
            else:
                _enqueue_analysis(app, rec_id)
    except Exception as e:  # noqa: BLE001
        _tb.print_exc()
        job["error"]  = str(e)
        job["status"] = "error"


@bp.route("/confirm", methods=["POST"])
@login_required
def confirm_ingest():
    """Validate, then run the copy + ingest as a background job — a big folder can
    take far longer than the webview's fetch timeout. Returns a job id to poll."""
    import threading
    import uuid
    data          = request.get_json() or {}
    source_folder = (data.get("source_folder_path") or "").strip()
    artist_name   = (data.get("artist_name") or "").strip()
    if not source_folder or not os.path.isdir(source_folder):
        return jsonify({"error": f"Source folder not found: {source_folder!r}"}), 400
    if not artist_name:
        return jsonify({"error": "artist_name is required"}), 400
    job_id = uuid.uuid4().hex
    _INGEST_JOBS[job_id] = {
        "status": "running", "copied": 0, "total": 0,
        "phase": "resolving", "phase_label": PHASES["resolving"],
        "started": time.time(), "phase_at": time.time(),
        "folder": os.path.basename(source_folder.rstrip("/")),
        "artist": artist_name,
        "quick": bool(data.get("skip_analysis")),
    }
    threading.Thread(
        target=_run_ingest_job,
        args=(job_id, current_app._get_current_object(), data, current_user.id),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id}), 202


@bp.route("/confirm/<job_id>", methods=["GET"])
@login_required
def confirm_status(job_id):
    """Poll a confirm job: running (+copy progress), or done+result / error."""
    job = _INGEST_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if job["status"] == "running":
        return jsonify({"status": "running", "copied": job.get("copied", 0),
                        "total": job.get("total", 0),
                        # Which of the four phases it is IN, so the dialog can
                        # say "Verifying checksums" instead of a frozen 0/0.
                        "phase": job.get("phase"),
                        "phase_label": job.get("phase_label"),
                        "phase_detail": job.get("phase_detail"),
                        "elapsed": round(time.time() - job.get("started", time.time()), 1),
                        "cancelling": bool(job.get("cancel"))})
    _INGEST_JOBS.pop(job_id, None)
    if job["status"] == "error":
        return jsonify({"status": "error", "error": job["error"]})
    if job["status"] == "cancelled":
        return jsonify({"status": "cancelled"})
    return jsonify({"status": "done", "result": job["result"]})


@bp.route("/pipeline", methods=["GET"])
@login_required
def pipeline_status():
    """
    Everything currently moving through ingestion, for the debug drawer's
    Ingest pane (Ryan, 2026-08-28: "add info to the debug window that
    characterizes and prints the status of the ingestion job").

    Reads live in-memory state — no DB, no filesystem — so it is safe to poll
    while a copy is saturating the disk. Confirm jobs are removed from
    _INGEST_JOBS the first time a client polls them to a terminal state, so
    this shows what is IN FLIGHT plus the analysis queue's running totals,
    which is the pair that answers "what is taking so long".
    """
    now = time.time()
    jobs = []
    for jid, j in list(_INGEST_JOBS.items()):
        jobs.append({
            "job_id":  jid[:8],
            "folder":  j.get("folder"),
            "artist":  j.get("artist"),
            "status":  j.get("status"),
            "phase":   j.get("phase"),
            "label":   j.get("phase_label"),
            "detail":  j.get("phase_detail"),
            "copied":  j.get("copied", 0),
            "total":   j.get("total", 0),
            "quick":   bool(j.get("quick")),
            "elapsed": round(now - j.get("started", now), 1),
            "in_phase": round(now - j.get("phase_at", now), 1),
        })
    return jsonify({"jobs": jobs, "analysis": analysis_snapshot()})


@bp.route("/confirm/<job_id>/cancel", methods=["POST"])
@login_required
def confirm_cancel(job_id):
    """
    Ask an in-flight ingest to stop.

    Cooperative, not a kill: the worker notices between files, undoes its own
    filesystem work and rolls back its uncommitted DB session. Recordings
    ingested earlier in a queue were committed by their own jobs and stay —
    cancelling stops the queue, it does not unwind history.
    """
    job = _INGEST_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    job["cancel"] = True
    return jsonify({"status": "cancelling"})


def _store_non_music_signal(tracks, library_root, folder_path):
    """
    Measure each track's spectral flatness and length, then score the recording.

    Writes ONLY the two raw measurements plus the derived score, and pointedly
    does NOT stamp `analysis_version` — these rows are partial. Stamping them
    would make analyse_and_store_track's skip-path treat them as fully analysed
    and never fill in the waveform, RMS and spectral columns that Complete mode
    (or a later Re-Analyze) is supposed to add.
    """
    from app.models.track_analysis import TrackAnalysis
    from app.utils.analysis import SIGNALS_ONLY_VERSION
    from app.utils.track_signals import (measure_track, non_music_scores,
                                         NON_MUSIC_THRESHOLD)

    measured = []
    # NOTE: only ever SET on rows this function creates. An existing row from a
    # real analysis keeps its own version, so adding the signal to an already
    # analysed recording never demotes it.
    for t in tracks:
        abs_path = os.path.join(library_root, folder_path, t.file_path)
        flat, dur = measure_track(abs_path)
        if flat is None and dur is None:
            continue
        ta = (db.session.query(TrackAnalysis)
              .filter_by(track_id=t.id).first())
        if ta is None:
            ta = TrackAnalysis(track_id=t.id)
            db.session.add(ta)
        ta.spectral_flatness = flat
        ta.duration_s = dur
        # Mark the row for what it is. A brand-new TrackAnalysis would
        # otherwise take the model's default of "1", which reads as "analysed
        # at version 1" — a row claiming an analysis that never ran. Any value
        # other than ANALYSIS_VERSION makes the full pass re-run later; this
        # one also says why the row exists.
        if ta.analysis_version in (None, "1"):
            ta.analysis_version = SIGNALS_ONLY_VERSION
        measured.append((t.id, flat, dur))

    if not measured:
        return
    db.session.flush()
    scored = non_music_scores(measured)
    for tid, hit in scored.items():
        ta = db.session.query(TrackAnalysis).filter_by(track_id=tid).first()
        if ta is not None:
            ta.non_music_score = hit["score"]
    log_step("ingest", "signals",
             f"{len(scored)} scored, "
             f"{sum(1 for h in scored.values() if h['score'] >= NON_MUSIC_THRESHOLD)}"
             f" look non-musical",
             force=True)


def _do_confirm(data, user_id, progress_cb=None, cancel_cb=None, phase_cb=None):
    """
    Resolve or create the full object chain, then ingest the recording.
    Runs inside an app context (background thread). Returns a result dict;
    raises on failure.

    Expected payload:
    {
      "source_folder_path": "/absolute/path/to/source",
      "artist_name":        "Grateful Dead",
      "start_year":         1972,
      "start_month":        9,
      "start_day":          3,
      "venue_name":         "CU Events Center",   # optional
      "city":               "Boulder",
      "state":              "CO",
      "country":            "US",                 # optional
      "source":             "SBD",
      "quality":            "B+",
      "lineage":            "...",
      "notes":              "",
      "is_complete":        true,
      "info_file_content":  "...",
      "event_name":         "Bonnaroo 2009",  # optional — name-resolved to Event record
      "event_id":           null,             # optional — use existing Event ID directly
      "ai_result":          {...},  # optional — raw AI Assist result if run pre-confirm,
                                     # saved as-is to ai_research_json (see run_ai_assist)
      "fingerprints":       [{"type":"ffp","filename":"...","content":"..."}],
      "tracks": [
        {"track_number":1,"title":"Dark Star","set_number":"Set 1","duration":1200,"filename":"t01.flac"}
      ]
    }
    """
    # No-op when nobody is listening, so every call site below stays unguarded.
    def _phase(key, detail=None):
        if phase_cb:
            phase_cb(key, detail)

    source_folder = (data.get("source_folder_path") or "").strip()
    artist_name   = (data.get("artist_name")        or "").strip()

    _phase("resolving", artist_name or None)

    city        = (data.get("city")       or "").strip() or None
    state       = (data.get("state")      or "").strip() or None
    country     = (data.get("country")    or "").strip() or None
    venue_name  = (data.get("venue_name") or "").strip() or None
    event_name  = (data.get("event_name") or "").strip() or None
    start_year  = data.get("start_year")
    start_month = data.get("start_month")
    start_day   = data.get("start_day")
    end_year    = data.get("end_year")
    end_month   = data.get("end_month")
    end_day     = data.get("end_day")

    # ── 1. Find or create Performer (the act) ─────────────────────────────────
    # `members`/`guests` are the Add Recording form's two personnel rows — see
    # app/utils/personnel.py::sync_performance_personnel for what they mean at
    # the PERFORMANCE level. Historically this block also used member_names to
    # seed/overwrite the act's ROSTER via set_performer_members, unconditionally,
    # for an existing Performer too — that's the same act-roster-corruption bug
    # Phase 1 already fixed for the recording page's PUT endpoint, just never
    # ported to ingest (flagged as an open gap in the design doc's ripple list,
    # item 5). resolve_or_create_performer(name, member_names) already has the
    # correct behavior baked in — it only seeds member_names as the roster when
    # the Performer is BRAND NEW, and leaves an existing act's roster alone —
    # so passing member_names straight through here fixes it with no new code.
    # Two different needs for the same payload key, so two variables:
    #  - member_names/guest_names (never None) for resolve_or_create_performer,
    #    which just wants "what to seed a BRAND NEW performer's roster with,
    #    if anything."
    #  - member_names_sync/guest_names_sync (RAW, preserves None) for
    #    sync_performance_personnel below, which treats None as "leave this
    #    bucket exactly as currently resolved" vs. [] as "the user cleared
    #    it, wipe it" (see that function's docstring). Batch Import's
    #    Auto-Ingest path (_batchIngestOne in app.js) never visits the review
    #    wizard, so it never sends "members"/"guests" at all — collapsing
    #    that omission to [] here made sync_performance_personnel think every
    #    inherited roster member had just been removed, which trips its
    #    case-5 safeguard: flip to 'explicit' and snapshot nothing, since
    #    nothing was in the list to keep. Net effect: the recording's
    #    Members row came out blank even though the performer's own roster
    #    was intact (Ryan, 2026-07-23 bug report — Bela Fleck & Tony
    #    Trischka). Only the manual Add Recording/Batch Review form pre-fills
    #    and always sends both keys (even an intentionally-emptied one), so
    #    that path's behavior is unchanged by this fix.
    member_names_sync = data.get("members")
    guest_names_sync  = data.get("guests")
    member_names = member_names_sync or []
    guest_names  = guest_names_sync  or []
    performer = resolve_or_create_performer(artist_name, member_names)

    # ── 3. Find or create Venue (optional) ────────────────────────────────────
    # Placeholder names ("Unknown Venue", "TBD", ...) are never linked as a
    # real Venue — they aren't one canonical physical place, they're a
    # stand-in every show without a known venue reuses. Linking them shares
    # one row's city/state/country across unrelated shows (Ryan's 2026-07-15
    # bug report; confirmed contamination in scripts/audit_placeholder_venues.py).
    # Treat exactly like no venue was given: venue stays None, and city/state/
    # country fall through to the Performance's own fallback fields below.
    venue = None
    venue_id_in = data.get("venue_id")
    if venue_id_in:
        # User selected an existing venue by id — use it, unless it resolves
        # to a placeholder row.
        candidate = db.session.get(Venue, int(venue_id_in))
        if candidate and not is_placeholder_venue_name(candidate.name):
            venue = candidate
    elif venue_name and not is_placeholder_venue_name(venue_name):
        # No id — look up by name or create new
        venue = db.session.query(Venue).filter(
            func.lower(Venue.name) == venue_name.lower()
        ).first()
        if not venue:
            venue = Venue(
                name    = venue_name,
                city    = city    or None,
                state   = state   or None,
                country = country or None,
            )
            db.session.add(venue)
            db.session.flush()

    # ── 3.5. Find or create Event (optional) ─────────────────────────────────
    event = None
    event_id_in = data.get("event_id")
    if event_id_in:
        event = db.session.get(Event, int(event_id_in))
    elif event_name:
        event = db.session.query(Event).filter(
            func.lower(Event.name) == event_name.lower()
        ).first()
        if not event:
            event = Event(
                name    = event_name,
                city    = city    or None,
                state   = state   or None,
                country = country or None,
            )
            db.session.add(event)
            db.session.flush()

    # ── 4. Find or create Performance ─────────────────────────────────────────
    perf_q = db.session.query(Performance).filter(
        Performance.performer_id == performer.id,
        Performance.start_year  == start_year,
        Performance.start_month == start_month,
        Performance.start_day   == start_day,
    )
    if venue:
        perf_q = perf_q.filter(Performance.venue_id == venue.id)

    performance = perf_q.first()
    if not performance:
        performance = Performance(
            performer_id = performer.id,
            venue_id     = venue.id  if venue  else None,
            event_id     = event.id  if event  else None,
            start_year   = start_year,
            start_month  = start_month,
            start_day    = start_day,
            end_year     = end_year,
            end_month    = end_month,
            end_day      = end_day,
            # Location fallback when no venue record
            city    = city    if not venue else None,
            state   = state   if not venue else None,
            country = country if not venue else None,
            # New performances start in the act's default resolution mode
            # (e.g. "Acoustic All-Stars" set to 'explicit' means every future
            # ingest starts explicit, not inherit) — see personnel.py.
            personnel_mode = performer.default_personnel_mode,
        )
        db.session.add(performance)
        db.session.flush()

    # Apply the Add Recording form's Members/Guests rows to THIS performance.
    # For the common case (new act, or an existing act's roster left
    # untouched in the form) this is a no-op — sync_performance_personnel
    # diffs against what's already resolved, and the form was pre-populated
    # from that same resolved state. It only actually writes rows when the
    # user edited something (added a guest, or removed a roster member for
    # this one show). Runs whether the Performance is brand new or an
    # already-existing one being re-confirmed with a second recording.
    sync_performance_personnel(performance, member_names_sync, guest_names_sync)

    # ── 5. Build canonical folder name ────────────────────────────────────────
    folder_name = build_folder_name(
        artist_name     = artist_name,
        start_year      = start_year,
        start_month     = start_month,
        start_day       = start_day,
        venue_name      = venue_name,
        city            = city,
        state           = state,
        country         = country,
        source          = data.get("source"),
    )

    # ── 6. Move / copy folder into library ────────────────────────────────────
    # Behavior precedence: explicit request payload → saved user preference →
    # DEFAULT "move" as of 2026-08-07 (Ryan). It was "copy" on the reasoning
    # that never destroying the source is the safe choice — but the failure it
    # actually produced was DUPLICATES: copy leaves the show sitting in the
    # import folder, a later re-scan offers it again, and nothing stops a
    # second ingest. Move makes re-ingesting the same files structurally
    # impossible, and the source is still recoverable from the library itself.
    behavior = (data.get("behavior") or "").strip().lower()
    if behavior not in ("move", "copy"):
        pref = db.session.query(UserPreference).filter_by(
            user_id=user_id, key="ingest_file_behavior"
        ).first()
        behavior = pref.value if pref else "move"
    library_root = str(current_app.config["LIBRARY_ROOT"])

    tracks_in = data.get("tracks", [])
    # Audio is always flattened + renamed into the library folder's root on
    # the way in (Ryan's 2026-07-14 decision — this is what fixes multi-disc
    # sources like CD1/CD2 whose per-disc TRACKNUMBER tags reset and collide).
    # Map is keyed by each track's ORIGINAL rel_path/filename as scanned;
    # move_to_library() applies it while copying/moving. Fingerprint files
    # (step 9) list the ORIGINAL names too, so matching happens against the
    # original name and only the final DB/verification path uses the new one.
    audio_rename_map = compute_audio_rename_map(tracks_in)

    # The long one. Named for what it will actually do to the source folder, so
    # "Moving files into the library" and "Copying files into the library" are
    # not the same sentence — one of them removes the original.
    _phase("moving" if behavior == "move" else "copying", folder_name)

    try:
        new_folder_path = move_to_library(
            source_folder    = source_folder,
            library_root     = library_root,
            artist_name      = artist_name,
            folder_name      = folder_name,
            behavior         = behavior,
            progress_cb      = progress_cb,
            audio_rename_map = audio_rename_map,
            cancel_cb        = cancel_cb,
        )
    except IngestCancelled:
        # A cancel is not a failure. move_to_library has already undone its own
        # filesystem work; re-raise unchanged so the job reports "cancelled"
        # rather than a scary "File operation failed".
        db.session.rollback()
        raise
    except Exception as e:
        db.session.rollback()
        raise RuntimeError(f"File operation failed: {e}")

    _phase("cataloging", f"{len(tracks_in)} track{'' if len(tracks_in) == 1 else 's'}")

    # ── 7. Create Recording ───────────────────────────────────────────────────
    rec_is_official = bool(data.get("is_official", False))
    # AI Assist may already have been run pre-confirm (Add Recording's own
    # "AI Assist" button, before the recording even exists) — if so, the
    # frontend sends the raw result back as "ai_result" so it isn't lost the
    # moment Confirm creates the row. Same shape/storage as the post-save
    # path (app.api.ingest._run_ai_job), just written synchronously here
    # instead of after a background job.
    ai_result = data.get("ai_result")
    rec = Recording(
        performance_id       = performance.id,
        source               = data.get("source"),
        lineage              = data.get("lineage"),
        quality              = data.get("quality"),
        is_complete          = data.get("is_complete", True),
        is_official          = rec_is_official,
        folder_path          = new_folder_path,
        original_folder_name = os.path.basename(source_folder),
        info_file_content    = data.get("info_file_content"),
        notes                = data.get("notes"),
        ai_research_json     = json.dumps(ai_result) if ai_result else None,
    )
    db.session.add(rec)
    db.session.flush()

    # ── 8. Create Tracks ──────────────────────────────────────────────────────
    # file_path stores the NEW flattened+renamed name (what move_to_library
    # actually wrote to disk), never the original subdir-nested scan name —
    # original names are kept around only for fingerprint matching (step 9).
    created_tracks     = []
    original_filenames = {}   # Track (by identity, filled in below) → original filename
    for t in tracks_in:
        # If recording is marked official, cascade to all tracks
        track_official = rec_is_official or bool(t.get("is_official", False))
        flags_raw      = t.get("flags") or []
        orig_filename  = t.get("filename", "")
        new_filename   = audio_rename_map.get(orig_filename, os.path.basename(orig_filename))
        track = Track(
            recording_id = rec.id,
            track_number = t.get("track_number"),
            title        = t.get("title") or f"Track {t.get('track_number', '?')}",
            set_number   = t.get("set_number") or None,
            duration     = t.get("duration"),
            file_path    = new_filename,
            is_official  = track_official,
            flags        = json.dumps(flags_raw) if flags_raw else None,
            songwriter   = t.get("songwriter") or None,
            notes        = t.get("notes") or None,
        )
        db.session.add(track)
        created_tracks.append(track)
        original_filenames[track] = orig_filename

    # ── 9. Fingerprint files: archive raw content, parse, match to tracks, and
    #       auto-verify. The files already sit in the library folder at this
    #       point (move_to_library has copied/moved the whole source tree, not
    #       just audio), so verification reads the exact copy the app streams
    #       from — no continued dependence on the original source folder.
    #
    #       Matching happens against tracks' ORIGINAL (pre-flatten/rename)
    #       filenames, since that's what the fingerprint file itself lists —
    #       a lightweight proxy stands in for each Track during the match so
    #       match_entries_to_tracks() (which reads `.file_path`/`.track_number`)
    #       sees the original name, then `.real` routes the matched checksum
    #       back to the actual Track, whose `.file_path` and on-disk location
    #       already reflect the new flattened name. Renaming doesn't affect
    #       the checksum itself — FFP/MD5/ST5 are content hashes.
    #
    #       Processed in FINGERPRINT_TYPE_PRIORITY order (ffp, then md5, then
    #       st5) so that when a folder has more than one fingerprint file,
    #       each track's stored status reflects the type most worth trusting
    #       rather than whichever happened to be listed last.
    fingerprints = sorted(
        data.get("fingerprints", []),
        key=lambda fp: FINGERPRINT_TYPE_PRIORITY.get(fp.get("type"), 9),
    )
    # Worth naming as its own phase: an ffp/st5 folder reads a FLAC header per
    # track and is instant, but an .md5 folder re-hashes every byte of every
    # file. Same phase, two very different waits — and until now both looked
    # like the copy having stalled at 100%.
    if fingerprints:
        _phase("checksums", ", ".join(sorted(
            {(fp.get("type") or "?").upper() for fp in fingerprints})))
    for fp in fingerprints:
        fp_type  = fp.get("type")
        rel_path = fp.get("rel_path") or os.path.basename(fp.get("filename") or "")
        fp_abs_path = os.path.join(library_root, new_folder_path, rel_path)
        content = None
        try:
            with open(fp_abs_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            pass
        db.session.add(RecordingFingerprint(
            recording_id     = rec.id,
            fingerprint_type = fp_type,
            filename         = fp.get("filename"),
            content          = content,
        ))
        if content and created_tracks:
            # A fingerprint file nested inside a disc/set subdir (e.g.
            # "CD1/checksum.md5") almost always lists filenames scoped to
            # that disc only ("01.flac", "02.flac", ...). Since audio is now
            # always flattened, those bare names can collide across discs —
            # every disc's own "01.flac" — so matching against ALL tracks
            # could hand a CD1 checksum to a CD2 track that happens to share
            # a basename. Restrict candidates to tracks whose ORIGINAL path
            # was under that same subdir; a root-level fingerprint file
            # (fp_dir == "") still considers every track, same as before.
            fp_dir = os.path.dirname(rel_path).replace(os.sep, "/")
            if fp_dir:
                candidates = [
                    t for t in created_tracks
                    if os.path.dirname(original_filenames.get(t, "")).replace(os.sep, "/") == fp_dir
                ]
                if not candidates:   # scoping found nothing usable — fall back
                    candidates = created_tracks
            else:
                candidates = created_tracks
            proxies = [
                _ChecksumMatchProxy(track, original_filenames.get(track, track.file_path))
                for track in candidates
            ]
            matches = match_entries_to_tracks(parse_checksum_file(content), proxies)
            now = datetime.now(timezone.utc)
            for proxy, expected in matches.items():
                track          = proxy.real
                track_abs_path = os.path.join(library_root, new_folder_path, track.file_path)
                track.checksum_type        = fp_type
                track.expected_checksum    = expected
                track.checksum_status      = verify_track_checksum(track_abs_path, fp_type, expected)
                track.checksum_verified_at = now

    # ── 10. Irrevocable ingest event ──────────────────────────────────────────
    db.session.add(RecordingEvent(
        recording_id = rec.id,
        user_id      = user_id,
        event_type   = "ingested",
        note         = f"behavior={behavior} original={os.path.basename(source_folder)}",
    ))

    # ── 11. Carry the Listening Quality analysis across ───────────────────────
    # MUST happen here, inside the ingest commit, and not lazily afterwards:
    # the staging row is keyed by the SOURCE folder path, and on a Move ingest
    # that folder no longer exists by now (move_to_library removed it, and the
    # empty-parent cleanup may have taken its parent too). This is the last
    # moment the association can be made.
    #
    # A no-op when the folder was never analysed — ingest does not require a
    # quality pass, and never failing an ingest over a score is deliberate.
    # ── 9.5. Non-music signal ────────────────────────────────────────────────
    # Runs HERE, inside the ingest, and not in the Librosa analysis pass, for
    # one reason: Quick Add is the default and Quick Add never runs that pass.
    # A signal that only exists in the mode nobody uses is not a feature.
    #
    # Affordable because it is windowed and librosa-free — measured at 0.15 s
    # per track, ~1.8 s for a 12-track show, against a copy that takes minutes.
    # Files are already in the library at this point (step 6 moved them), so it
    # reads the copy it will be describing, not the source that may be gone.
    #
    # Never fatal: an ingest must not fail over a suggestion.
    _phase("signals", f"{len(created_tracks)} tracks")
    try:
        _store_non_music_signal(created_tracks, library_root, new_folder_path)
    except Exception:  # noqa: BLE001
        import traceback as _tb3
        _tb3.print_exc()

    _phase("saving")
    try:
        from app.utils.quality_store import promote_to_recording
        promote_to_recording(source_folder, rec.id, commit=False)
    except Exception:  # noqa: BLE001
        import traceback as _tb2
        _tb2.print_exc()

    db.session.commit()

    checksum_mismatches = sum(1 for t in created_tracks if t.checksum_status == "mismatch")

    return {
        "recording_id":        rec.id,
        "performer_id":        performer.id,
        "folder_name":         folder_name,
        "event_id":            event.id if event else None,
        "checksum_mismatches": checksum_mismatches,
    }


import re as _re
from collections import Counter as _Counter

_TEXT_EXTS   = {'.txt', '.nfo', '.md', '.text', '.log'}
_LOSSY_EXTS  = {'.mp3', '.aac', '.ogg', '.m4a'}
_LOSSLESS_EXTS = {'.flac', '.wav', '.aiff', '.aif', '.ape', '.wv'}
_DATE_RE     = _re.compile(r'\b(19|20)\d{2}[-._](0[1-9]|1[0-2])[-._](0[1-9]|[12]\d|3[01])\b')
_TRACK_NUM_RE = _re.compile(r'^(?:track\s*)?(\d{1,3})[.\s\-_]', _re.IGNORECASE)


def _audit_incoming_folder(folder_path):
    """
    Lightweight walk of an incoming folder. Returns (audio_count, size_mb, issues).
    No parsing of file content — pure filesystem inspection.
    """
    issues   = []
    ext_sets = _Counter()   # ext → count of audio files with that ext
    total_bytes = 0
    has_text    = False
    track_nums  = []
    subdir_count = 0        # how many subdirs contain audio
    audio_in_root = 0

    for root, dirs, files in os.walk(folder_path):
        dirs.sort()
        depth = root[len(folder_path):].count(os.sep)
        has_audio_here = False

        for fname in files:
            ext  = os.path.splitext(fname)[1].lower()
            fpath = os.path.join(root, fname)
            try:
                fsize = os.path.getsize(fpath)
            except OSError:
                fsize = 0

            if ext in _AUDIO_EXTS:
                ext_sets[ext] += 1
                total_bytes   += fsize
                has_audio_here = True
                if depth == 0:
                    audio_in_root += 1
                # try to parse track number from filename
                m = _TRACK_NUM_RE.match(fname)
                if m:
                    track_nums.append(int(m.group(1)))

            if ext in _TEXT_EXTS:
                has_text = True

        if has_audio_here and depth > 0:
            subdir_count += 1

    audio_count = sum(ext_sets.values())
    size_mb     = round(total_bytes / (1024 * 1024), 1)

    # ── Issue checks ──────────────────────────────────────────────────────────

    if audio_count == 0:
        issues.append({"severity": "error", "msg": "No audio files"})
        return audio_count, size_mb, issues

    # Format checks
    exts_present = set(ext_sets.keys())
    lossy  = exts_present & _LOSSY_EXTS
    flacs  = ext_sets.get('.flac', 0)
    wavs   = ext_sets.get('.wav', 0) + ext_sets.get('.aiff', 0) + ext_sets.get('.aif', 0)

    if lossy:
        labels = '/'.join(e.lstrip('.').upper() for e in sorted(lossy))
        issues.append({"severity": "warn", "msg": f"Lossy format ({labels})"})
    elif wavs and not flacs:
        issues.append({"severity": "warn", "msg": "WAV/AIFF — not FLAC"})
    elif wavs and flacs:
        issues.append({"severity": "warn", "msg": "Mixed FLAC + WAV"})
    elif len(exts_present & _LOSSLESS_EXTS) > 1:
        issues.append({"severity": "warn", "msg": "Mixed lossless formats"})

    # Text file
    if not has_text:
        issues.append({"severity": "warn", "msg": "No text file"})

    # Track numbering
    if track_nums:
        dupes = [n for n, c in _Counter(track_nums).items() if c > 1]
        if dupes:
            issues.append({"severity": "warn",
                           "msg": f"Duplicate track numbers: {sorted(dupes)}"})
        else:
            nums = sorted(track_nums)
            expected = list(range(nums[0], nums[0] + len(nums)))
            gaps = sorted(set(expected) - set(nums))
            if gaps:
                issues.append({"severity": "warn",
                               "msg": f"Track number gap(s): {gaps}"})
    else:
        # Audio files but none have leading track numbers
        issues.append({"severity": "info", "msg": "No track numbers in filenames"})

    # Multi-set / multi-disc detection
    if subdir_count >= 2:
        issues.append({"severity": "info",
                       "msg": f"Multi-set ({subdir_count} subdirs with audio)"})
    elif subdir_count == 1 and audio_in_root == 0:
        # All audio in a single subdir — usually fine but worth flagging
        issues.append({"severity": "info", "msg": "Audio in subdir"})

    # Folder name — should contain a date
    fname = os.path.basename(folder_path)
    if not _DATE_RE.search(fname):
        issues.append({"severity": "info", "msg": "No date in folder name"})

    return audio_count, size_mb, issues


@bp.route("/batch-scan", methods=["POST"])
@login_required
def batch_scan():
    """
    POST /api/ingest/batch-scan
    Walk a source directory, scan every show subfolder, and return a
    confidence-tiered list of candidates for batch ingest.

    Request body:
      { "source_dir": "/absolute/path/to/Import Processed" }

    Response: list of candidates, each with:
      - name, path, audio_count, size_mb, issues
      - health: compute_health() result — { score, band, factors, ... }. This
                IS the completeness score shown on each row.
      - tier: "green" | "yellow" | "red" — literally health["band"]. Used to be
              a second, independently-derived heuristic off conf_artist/
              conf_date/conf_tracks, which could disagree with the visible
              score (Ryan hit this 2026-07-16: a 94-scoring row still bucketed
              under "yellow"). Now it's just an alias so the pill counts and
              the "Auto-Ingest All ___" filters always match what's on screen.
      - confidence: { artist, date, tracks, venue }  — per-field scores, still
                    used for the "uncertain" styling on individual fields in
                    the expanded row detail (unrelated to tier now).
      - extracted: { artist, year, month, day, venue, city, state, country,
                     source, lineage, track_count, tracks_titled }
      - paula: compute_paula_score() result, or None (empty/unreadable folder).
               Purple-border source-of-truth in Add Recording's field-level
               confidence highlighting — no longer rendered as its own
               narrative/avatar anywhere in the UI (removed 2026-07-16).
      - already_ingested: bool  (folder path already in DB)
    """
    from app.utils.ingest import build_scan_payload
    from app.models.recording import Recording
    from app.utils.paula import compute_paula_score

    data       = request.get_json() or {}
    source_dir = (data.get("source_dir") or "").strip()

    if not source_dir or not os.path.isdir(source_dir):
        return jsonify({"error": f"Directory not found: {source_dir!r}"}), 400

    # Umbrella job for the whole batch — each individual folder ALSO gets its
    # own "scan:<folder_path>" job from build_scan_payload() itself, so a
    # hang shows both "batch is on folder 3/6" and exactly which phase of
    # THAT folder's scan is stuck.
    batch_job = f"batch-scan:{source_dir}"
    log_step(batch_job, "start", "POST /api/ingest/batch-scan")

    # Known artist/venue records — same lookups the interactive Add Recording
    # scan uses, so Paula's per-item confidence scoring here (added 2026-07-15,
    # Ryan: "let's get her pulled into that experience") matches exactly.
    known_performers = [p.name for p in db.session.query(Performer.name).all()]
    known_venues = [
        {"name": v.name, "city": v.city, "state": v.state, "country": v.country}
        for v in db.session.query(Venue).all()
    ]

    # Already-ingested folder paths (relative or basename match)
    ingested_paths = {
        os.path.basename(r.folder_path)
        for r in db.session.query(Recording.folder_path).all()
    }

    results = []

    # Resolve every top-level entry to its actual show paths before scanning.
    # Handles arbitrary nesting depth (artist → year → show, etc.).
    #
    # Hoisted into utils/ingest.py on 2026-07-30 — the Listening Quality
    # analyser resolves shows through the SAME function, so the triage list and
    # the metadata list can never disagree about which folders are shows.
    from app.utils.ingest import resolve_shows_in_dir
    show_paths = resolve_shows_in_dir(source_dir)
    log_step(batch_job, "resolved shows", f"{len(show_paths)} folder(s) to scan")

    for i, folder_path in enumerate(show_paths):
        log_step(batch_job, "scanning folder",
                 f"{i + 1}/{len(show_paths)}: {os.path.basename(folder_path)}")
        already_ingested = os.path.basename(folder_path) in ingested_paths

        # ── Filesystem audit ──────────────────────────────────────────────────
        audio_count, size_mb, issues = _audit_incoming_folder(folder_path)

        if audio_count == 0:
            results.append({
                "name": os.path.basename(folder_path), "path": folder_path,
                "audio_count": 0, "size_mb": size_mb,
                "tier": "red", "issues": issues,
                "confidence": {}, "extracted": {},
                "health": {"score": 0, "band": "red", "factors": [], "populated": 0, "total": 0},
                "paula": None,
                "already_ingested": already_ingested,
            })
            continue

        # ── Full scan (tags + info file) ───────────────────────────────────────
        # Same build_scan_payload() the Add Recording flow uses, so this folder's
        # health score and field suggestions are identical no matter which flow
        # scanned it — no separate hand-rolled parsing to drift out of sync.
        try:
            scan = build_scan_payload(folder_path)
        except Exception:
            scan = None

        from_tags = ((scan or {}).get("suggestions") or {}).get("from_tags") or {}
        from_info = ((scan or {}).get("suggestions") or {}).get("from_info_file") or {}
        health    = (scan or {}).get("health") or {"score": 0, "band": "red"}

        # Paula's per-item confidence read — same engine as the interactive
        # scan endpoint (app/api/recordings.py). Frontend aggregates these
        # into a single batch-level narrative rather than showing per-row
        # scores (Ryan didn't want per-item Paula numbers cluttering the list).
        paula_result = None
        if scan:
            try:
                paula_result = compute_paula_score(scan, known_performers, known_venues)
            except Exception:
                paula_result = None

        # ── Field resolution: tags win, info file fills gaps ──────────────────
        # Artist
        artist = (from_tags.get("artist") or from_info.get("artist") or "").strip()
        artist_in_db = bool(
            artist and any(
                artist.lower() == p.lower() for p in known_performers
            )
        )
        artist_fuzzy = bool(from_info.get("artist_match"))

        # Date — prefer CONCERTDATE tag, then individual fields from info file
        concert_date_tag = from_tags.get("concert_date") or ""
        year = month = day = None
        if concert_date_tag:
            # Parse "YYYY-MM-DD" or "YYYY-MM" or "YYYY"
            import re as _re2
            m = _re2.match(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", concert_date_tag)
            if m:
                year  = int(m.group(1))
                month = int(m.group(2)) if m.group(2) else None
                day   = int(m.group(3)) if m.group(3) else None
        if not year:
            year  = from_info.get("year")
            month = from_info.get("month")
            day   = from_info.get("day")
        # Also try folder name if still no date
        if not year:
            m = _DATE_RE.search(os.path.basename(folder_path))
            if m:
                year  = int(m.group(0)[:4])
                month = int(m.group(0)[5:7])
                day   = int(m.group(0)[8:10])

        # Tracks
        tag_trks = from_tags.get("tracks", [])
        titled_count = sum(
            1 for t in tag_trks if t.get("title") and t["title"].strip()
        )
        info_tracks  = from_info.get("tracks", [])

        # Merged per-track inferred title (tag wins, info file fills gap) —
        # the exact same resolution _batchIngestOne() uses when it actually
        # writes tracks, so what's previewed here is what Auto-Ingest would do.
        merged_tracks = []
        for idx in range(audio_count):
            tag_t  = tag_trks[idx]    if idx < len(tag_trks)    else {}
            info_t = info_tracks[idx] if idx < len(info_tracks) else {}
            tag_title  = (tag_t.get("title") or "").strip()
            info_title = (info_t.get("title") or "").strip()
            merged_tracks.append({
                "number": idx + 1,
                "title":  tag_title or info_title or None,
                "source": "tags" if tag_title else ("info" if info_title else None),
            })

        # Venue / location — build_scan_payload already parses the tag's
        # CONCERTLOCATION into city/state/country via the shared parser.
        venue   = (from_tags.get("venue")   or from_info.get("venue")   or "").strip() or None
        city    = (from_tags.get("city")    or from_info.get("city")    or "").strip() or None
        state   = (from_tags.get("state")   or from_info.get("state")   or "").strip() or None
        country = (from_tags.get("country") or from_info.get("country") or "").strip() or None

        source  = (from_tags.get("source")  or from_info.get("source")  or "").strip() or None
        lineage = (from_tags.get("lineage") or from_info.get("lineage") or "").strip() or None

        # ── Confidence scoring ────────────────────────────────────────────────
        # Each dimension: "high" | "medium" | "low"

        # Artist confidence — measures name clarity, not DB presence
        # (bulk import creates new artists; DB match is a nice-to-have signal only)
        if artist_in_db:
            conf_artist = "high"    # exact DB match
        elif artist and artist_fuzzy:
            conf_artist = "medium"  # fuzzy DB match
        elif artist:
            conf_artist = "low"     # name found in tags/folder, not yet in DB
        else:
            conf_artist = "none"    # no artist name found at all → blocks green

        # Date confidence
        if year and month and day:
            conf_date = "high"
        elif year and month:
            conf_date = "medium"
        elif year:
            conf_date = "low"
        else:
            conf_date = "none"

        # Track title confidence
        if titled_count == audio_count and audio_count > 0:
            conf_tracks = "high"
        elif titled_count > 0:
            conf_tracks = "medium"
        elif info_tracks:
            conf_tracks = "medium"   # info file has titles even if tags don't
        else:
            conf_tracks = "low"

        # Venue confidence (nice-to-have, doesn't block green)
        conf_venue = "high" if venue else "low"

        # ── Tier assignment ───────────────────────────────────────────────────
        # Tier is just the completeness-score band (health.band, computed above
        # from the same compute_health() shown as the row's score badge). Used
        # to be a second, independently-derived heuristic off conf_artist/
        # conf_date/conf_tracks — that produced a real inconsistency Ryan hit
        # 2026-07-16: a row could show "94" (health/completeness score, green
        # band) while still being bucketed under the yellow pill count/filter,
        # because the two scorers didn't always agree. One score, one band, no
        # more double bookkeeping. conf_* values are kept as-is — they still
        # drive the per-field "uncertain" styling in the expanded row detail.
        tier = health["band"]

        results.append({
            "name":        os.path.basename(folder_path),
            "path":        folder_path,
            "audio_count": audio_count,
            "size_mb":     size_mb,
            "tier":        tier,
            "issues":      issues,
            "health":      health,
            "paula":       paula_result,
            "confidence": {
                "artist": conf_artist,
                "date":   conf_date,
                "tracks": conf_tracks,
                "venue":  conf_venue,
            },
            "extracted": {
                "artist":        artist or None,
                "year":          year,
                "month":         month,
                "day":           day,
                "venue":         venue,
                "city":          city,
                "state":         state,
                "country":       country,
                "source":        source,
                "lineage":       lineage,
                "track_count":      audio_count,
                "tracks_titled":    titled_count,
                "info_track_count": len(info_tracks),  # total from info file (not capped)
                "tracks":           merged_tracks,
            },
            "already_ingested": already_ingested,
        })

    # Drop anything already in the DB — a folder that's been ingested (via this
    # batch UI, the full wizard, or otherwise) shouldn't keep showing up as a
    # pending candidate on a rescan.
    results = [r for r in results if not r["already_ingested"]]

    # Sort: highest completeness score first; alpha within a score for stability.
    results.sort(key=lambda r: (-r["health"]["score"], r["name"].lower()))

    green  = sum(1 for r in results if r["tier"] == "green")
    yellow = sum(1 for r in results if r["tier"] == "yellow")
    red    = sum(1 for r in results if r["tier"] == "red")

    log_step(batch_job, "done", f"{len(results)} folder(s) scored "
                                 f"({green} green, {yellow} yellow, {red} red)")

    return jsonify({
        "source_dir": source_dir,
        "total":  len(results),
        "green":  green,
        "yellow": yellow,
        "red":    red,
        "items":  results,
    })


# NOTE: the standalone _incoming/ queue was removed — per-recording health is now
# surfaced in the Add Recording review step (see compute_health). _audit_incoming_folder
# is retained (used by batch-scan's filesystem audit).
