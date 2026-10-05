"""
api/ingest.py — Full ingest confirmation endpoint.

POST /api/ingest/confirm

Handles the full "resolve or create" chain from a single payload:
  Artist → Venue (optional) → Performance → Recording + Tracks

This avoids the frontend needing to pre-resolve IDs. The user just
provides names and dates; this endpoint does the lookup/create work.
"""

import re
import os
import json
import queue
import collections
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user
from sqlalchemy import func

_AUDIO_EXTS = {'.flac', '.mp3', '.wav', '.aiff', '.aif', '.m4a', '.ogg', '.ape', '.wv'}

from app.extensions import db
from app.models.artist import Artist
from app.models.musician import Musician, Membership
from app.utils.artists import mark_artist_confirmed, resolve_or_create_artist
from app.utils.personnel import sync_performance_personnel
from app.utils.venues import is_placeholder_venue_name
from app.models.venue import Venue
from app.models.event import Event
from app.utils.event_names import clean_event_name, event_key
from app.models.genre import Genre
from app.models.performance import Performance
from app.models.recording import Recording, RecordingFingerprint
from app.models.recording_event import RecordingEvent
from app.models.track import Track
from app.utils.ingest import (move_to_library, compute_audio_rename_map,
                              resolve_ingest_file_path, write_flac_tags,
                              IngestCancelled, scan_folder, build_scan_payload)
from app.utils.bulk_ingest import (folder_format, _READABLE_AUDIO_EXTS,
                                   CONVERTIBLE_AUDIO_EXTS)
from app.utils.resolve import resolve, find_duplicates, verdict, Field as ResolvedField
from app.utils.file_naming import flattens
from app.utils.folder_naming import build_folder_name
from app.utils.ai_assist import run_ai_assist, AiAssistError
from app.utils.prefs import get_api_key, get_pref
from app.utils import node_settings
from app.utils.health import compute_health
from app.utils.checksums import (
    parse_checksum_file, match_entries_to_tracks, verify_track_checksum,
    FINGERPRINT_TYPE_PRIORITY, ChecksumMatchProxy,
)
from app.utils.debug_log import log_step

bp = Blueprint("ingest", __name__)


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


def _run_ai_job(job_id, folder_path, current, api_key, model, *, recording_id=None,
                app=None, question=None, prior=None):
    import time as _time
    import traceback as _tb
    t0 = _time.time()
    try:
        result = run_ai_assist(folder_path, current, api_key, model,
                               question=question, prior=prior)
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


# resolve_similar_artist_ids / _act_key / _ARTIST_SIMILARITY / _ACT_NOISE_WORDS
# moved to app/utils/artists.py (Ingest Field Resolver spec v1 section 6 --
# the route layer imports utils, not the other way round). Re-imported here
# so every existing call site in this file (and `from app.api.ingest import
# resolve_similar_artist_ids` elsewhere) keeps working unchanged.
from app.utils.artists import (resolve_similar_artist_ids, _act_key,
                               _ARTIST_SIMILARITY, _ACT_NOISE_WORDS)


@bp.route("/check-existing", methods=["GET"])
@login_required
def check_existing():
    """
    GET /api/ingest/check-existing?artist_name=...&year=...&month=...&day=...
    Read-only lookup (no creation) — the Add Recording form calls this once
    artist + date are known, to WARN (not block) when the library already
    has a performance for that artist/date. Multiple recordings per
    performance are legitimate (SBD + AUD of the same show), so this never
    prevents Confirm — it just surfaces what's already there so an archivist
    doesn't accidentally re-ingest a tape they already have. Ryan, 2026-07-14.

    Matches on artist name the same way resolve_or_create_artist does
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
        return jsonify({"artist_found": False, "performances": []})

    artist_ids = resolve_similar_artist_ids(artist_name)
    if not artist_ids:
        return jsonify({"artist_found": False, "performances": []})

    q = db.session.query(Performance).filter(
        Performance.artist_id.in_(artist_ids),
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
            "artist": p.artist.name if p.artist else None,
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
    return jsonify({"artist_found": True, "performances": performances})


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

    Body: { folder_path, current: {...}, question?: "..." }

    `question` is the archivist's optional pre-run question. It rides in this
    existing body rather than getting an endpoint of its own — a question asked
    BEFORE the run is just more context for the one call we were already
    making, which is the whole reason it was chosen over a chat surface
    (Ryan, 2026-09-07).
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
        kwargs={"question": data.get("question")},
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

    data = request.get_json(silent=True) or {}
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    p = rec.performance
    v = p.venue if p else None
    current = {
        "artist":  (p.artist.name if (p and p.artist) else ""),
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

    # A re-run gets the PREVIOUS run's findings back (see _prior_summary): the
    # saved blob is right here, and without it a second pass re-searches
    # everything the first one already found and is free to contradict it.
    prior = None
    if rec.ai_research_json:
        try:
            prior = json.loads(rec.ai_research_json)
        except (ValueError, TypeError):
            prior = None   # a corrupt blob is a reason to skip the recap, not to fail the run

    job_id = uuid.uuid4().hex
    _AI_JOBS[job_id] = {"status": "running"}
    threading.Thread(
        target=_run_ai_job,
        args=(job_id, rec.folder_path or "", current, api_key, model),
        kwargs={"recording_id": recording_id, "app": current_app._get_current_object(),
                "question": data.get("question"), "prior": prior},
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
    "tags":       "Writing tags to files",
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
# Follow-up kinds. The queue is in memory (every kind is re-derived from DB
# state by enqueue_followups(), so nothing needs to survive a restart).
#   "audio"      ONE audio pass for a recording: Listening Quality score +
#                non-music signal + Full (librosa) analysis. Live only when
#                enqueued automatically; see run_audio_pass().
#   "mb_artist"  a MusicBrainz lookup for one artist still at "never looked
#                up", deferred off the interactive ingest path; or, for a
#                matched artist with no genre, one genre fetch (2026-10-05).
#   "mb_release" a MusicBrainz release lookup for one studio recording
#                still at "never looked up" (mb_release_status IS NULL).
#                Never enqueued for a live recording.
#   "images"     artwork scan for a recording never scanned for it.
#
# Two lanes, one worker thread each: "audio" (CPU/disk-bound decode) and
# "net" (MusicBrainz + artwork). They are independent, so a slow decode never
# holds up MusicBrainz and vice versa; "net" being a single thread is what keeps
# MusicBrainz's rate limit respected.
#
# Small runs first: inside a lane, pending work is grouped by the Bulk Ingest
# run it belongs to (None = single-folder ingest / no run) and the group with
# the fewest outstanding items is served next, FIFO inside a group. Work with
# no run always goes first. That way one folder ingested while a 2000-show run
# is draining is not stuck behind that run's backlog.
#
# _QUEUED_KEYS / _PENDING_BY_KIND answer "what's in the queue right now" and
# dedupe a repeat enqueue_followups() call against work already waiting.
_Q_LOCK = threading.Lock()   # guards _QUEUED_KEYS / _PENDING_BY_KIND (two workers + request threads)


class _Lane:
    """A blocking queue ordered by run size (see above) instead of arrival."""

    def __init__(self):
        self._cv = threading.Condition()
        self._by_run = {}    # run_key (run id or None) -> deque of (kind, id)

    def put(self, key, run_key=None):
        with self._cv:
            self._by_run.setdefault(run_key, collections.deque()).append(key)
            self._cv.notify()

    def _pop(self):
        if None in self._by_run:
            rk = None
        else:
            rk = min(self._by_run, key=lambda r: (len(self._by_run[r]), r))
        dq = self._by_run[rk]
        item = dq.popleft()
        if not dq:
            del self._by_run[rk]
        return item

    def get(self):
        with self._cv:
            while not self._by_run:
                self._cv.wait()
            return self._pop()

    def get_nowait(self):
        with self._cv:
            if not self._by_run:
                raise queue.Empty
            return self._pop()

    def discard(self, key):
        """Drop a queued (not yet started) key. True if it was there."""
        with self._cv:
            for rk, dq in list(self._by_run.items()):
                if key in dq:
                    dq.remove(key)
                    if not dq:
                        del self._by_run[rk]
                    return True
        return False

    def qsize(self):
        with self._cv:
            return sum(len(d) for d in self._by_run.values())


_LANES = {"audio": _Lane(), "net": _Lane()}
# N9: guards the check-and-start of the worker threads (_ANALYSIS_STATE["worker"]).
_WORKER_START_LOCK = threading.Lock()
_ANALYSIS_STATE = {
    "current": None,      # {recording_id, name, started, tracks_total} -- audio lane
    "worker":  False,     # have the worker threads been started
    "done":    0,
    "failed":  0,
}
_QUEUED_KEYS = set()
_PENDING_BY_KIND = {"audio": 0, "mb_artist": 0, "mb_release": 0, "images": 0}

# Kinds an older build queued. Folded into "audio" so nothing crashes on them.
_LEGACY_AUDIO_KINDS = ("analysis", "score", "signals")


def _normalize_kind(kind):
    return "audio" if kind in _LEGACY_AUDIO_KINDS else kind


def _lane_name(kind):
    return "audio" if kind == "audio" else "net"


def _enqueue(app, kind, item_id, run_id=None):
    """
    Put one (kind, item_id) item on its lane, starting the worker threads on
    first use. Deduped against whatever is already queued (a legacy kind is
    folded into "audio" first, so it dedupes per recording).

    `run_id` is the Bulk Ingest run the item belongs to (None for none); it
    only decides ordering, see the lane comment above.

    Returns True if the item was newly queued, False if it was already there.
    """
    kind = _normalize_kind(kind)
    key = (kind, item_id)
    with _Q_LOCK:
        if key in _QUEUED_KEYS:
            return False
        _QUEUED_KEYS.add(key)
        _PENDING_BY_KIND[kind] = _PENDING_BY_KIND.get(kind, 0) + 1
    with _WORKER_START_LOCK:
        if not _ANALYSIS_STATE["worker"]:
            _ANALYSIS_STATE["worker"] = True
            for lane_name in ("audio", "net"):
                threading.Thread(target=_followup_worker, args=(app, lane_name),
                                 daemon=True).start()
    _LANES[_lane_name(kind)].put(key, run_id)
    return True


class MoveFailed(RuntimeError):
    """move_to_library hit an OSError (possibly after moving some files)."""


# One audio pass per recording at a time: the audio lane and Analyze Audio
# (/reprocess) could otherwise decode and write the same recording's rows
# concurrently. Locks are created on demand and never removed (one small
# object per recording ever analysed).
_AUDIO_LOCKS = {}
_AUDIO_LOCKS_GUARD = threading.Lock()


def _audio_lock(recording_id):
    with _AUDIO_LOCKS_GUARD:
        return _AUDIO_LOCKS.setdefault(recording_id, threading.Lock())


def drop_queued_audio(recording_id):
    """Remove a queued, not-yet-started audio follow-up for `recording_id`
    (Analyze Audio just did the work). A no-op when nothing is queued."""
    key = ("audio", recording_id)
    if _LANES["audio"].discard(key):
        with _Q_LOCK:
            _QUEUED_KEYS.discard(key)
            _PENDING_BY_KIND["audio"] = max(0, _PENDING_BY_KIND.get("audio", 0) - 1)


def run_audio_pass(recording_id, score=True, reanalyze=False):
    """Serialised per recording; see _run_audio_pass for what it does."""
    with _audio_lock(recording_id):
        return _run_audio_pass(recording_id, score=score, reanalyze=reanalyze)


def _run_audio_pass(recording_id, score=True, reanalyze=False):
    """
    The whole audio pass for one recording: Listening Quality score, non-music
    signal, Full (librosa) analysis. Callable for ANY recording; the live-only
    rule belongs to enqueue_followups()/_handle_audio, not here (the Analyze
    Audio button runs it on albums too, with score forced).

    score=False skips only the Listening Quality part (it already exists).
    reanalyze=True redoes tracks that already have a current analysis.

    The three parts are independent: a failure in one is recorded and the
    rest still run. Returns {"score", "signals", "analysis", "errors"}; each
    part is "ok" / "skipped" / "failed", errors is a list of strings.
    """
    import traceback as _tb
    from app.models.recording import Recording
    from app.models.track_analysis import TrackAnalysis

    out = {"score": "skipped", "signals": "skipped", "analysis": "skipped", "errors": []}
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return out
    library_root = current_app.config.get("LIBRARY_ROOT", "")
    folder_path = rec.folder_path or ""

    # 1. Listening Quality. Same scorer chain as manual triage; unreadable
    # audio returns an {"error"} dict and no row is written, so the recording
    # stays unscored and a later enqueue_followups() retries it.
    if score:
        try:
            from app.utils.quality import (extract_recording_features, score_recording,
                                           guess_source_from_name)
            from app.utils.quality_store import upsert_for_recording
            features = extract_recording_features(os.path.join(library_root, folder_path))
            if "error" in features:
                out["score"] = "failed"
                out["errors"].append(f"score: {features['error']}")
            else:
                name = os.path.basename(folder_path.rstrip("/"))
                scored = score_recording(features, source=guess_source_from_name(name))
                upsert_for_recording(recording_id, scored, features)
                out["score"] = "ok"
        except Exception as e:  # noqa: BLE001 -- parts must not block each other
            db.session.rollback()
            _tb.print_exc()
            out["score"] = "failed"
            out["errors"].append(f"score: {e}")

    # 2. Non-music signal (cheap, windowed, librosa-free). Skipped when a
    # track already carries one, unless re-analysing.
    try:
        has_signal = (db.session.query(TrackAnalysis.id)
                      .join(Track, Track.id == TrackAnalysis.track_id)
                      .filter(Track.recording_id == recording_id,
                              TrackAnalysis.non_music_score.isnot(None))
                      .first())
        if reanalyze or not has_signal:
            _store_non_music_signal(rec.tracks, library_root, folder_path)
            db.session.commit()   # the helper only flushes
            out["signals"] = "ok"
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        _tb.print_exc()
        out["signals"] = "failed"
        out["errors"].append(f"signals: {e}")

    # 3. Full analysis. Re-fetch rec: a rollback above expires it.
    try:
        from app.utils.analysis import analyse_recording
        rec = db.session.get(Recording, recording_id)
        n_ok, errors = analyse_recording(rec, library_root, db.session,
                                         reanalyze=reanalyze)
        out["analysis"] = "failed" if errors and n_ok == 0 else "ok"
        out["errors"].extend(f"analysis: {f}: {m}" for f, m in errors)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        _tb.print_exc()
        out["analysis"] = "failed"
        out["errors"].append(f"analysis: {e}")
    return out


def _handle_audio(recording_id):
    """
    Follow-up handler for "audio": live recordings only (an album is never
    auto-scored or analysed). Scoring is skipped when a Listening Quality
    result already exists, which is the case after a Review First run scored
    the folder before ingest; signals and Full analysis still run.
    """
    from app.models.recording import Recording
    from app.utils.quality_store import get_for_recording

    rec = db.session.get(Recording, recording_id)
    if not rec or rec.kind == "studio":
        return
    row = get_for_recording(recording_id)
    has_score = row is not None and row.listening_quality is not None
    _ANALYSIS_STATE["current"] = {
        "recording_id": recording_id,
        "name": os.path.basename(rec.folder_path or "") or str(recording_id),
        "started": time.time(),
        "tracks_total": len(rec.tracks or []),
    }
    log_step(f"audio:{recording_id}", "analyzing",
             f"{len(rec.tracks or [])} tracks", force=True)
    try:
        res = run_audio_pass(recording_id, score=not has_score)
        _ANALYSIS_STATE["done"] += 1
        if res["errors"]:
            _ANALYSIS_STATE["failed"] += len(res["errors"])
        print("[ingest] audio pass for recording %s: %s" % (recording_id, res), flush=True)
    finally:
        _ANALYSIS_STATE["current"] = None


def _handle_mb_artist(artist_id):
    """
    MusicBrainz lookup for one artist, only if nobody has looked it up since
    it was enqueued. Uses try_match_artist() exactly as resolve_or_create_artist
    does at ingest time -- same gate, same circuit breaker, same "never under
    TESTING" switch -- so this is never a second code path to keep in sync.
    """
    from app.models.artist import Artist
    from app.utils import musicbrainz as _mb

    artist = db.session.get(Artist, artist_id)
    if not artist:
        return
    if artist.mb_status is None:
        _mb.try_match_artist(artist)
    elif _mb.owes_artist_genre(artist):
        _mb.fill_artist_genre(artist)
    else:
        return
    db.session.commit()


def _handle_mb_release(recording_id):
    """
    MusicBrainz release lookup for one studio recording, only if nobody has
    looked it up since it was enqueued. Uses try_match_release() exactly as
    the automatic pass does -- same gate, same circuit breaker, same
    "never under TESTING" switch -- so this is never a second code path to
    keep in sync.
    """
    from app.models.recording import Recording
    from app.utils import musicbrainz as _mb

    rec = db.session.get(Recording, recording_id)
    if not rec or rec.mb_release_status is not None:
        return
    _mb.try_match_release(rec)
    db.session.commit()


def _handle_images(recording_id):
    """
    Artwork backfill for one recording that has never been scanned for it
    (S8, independent review v1). Only on-demand path before this was manual
    upload -- a recording ingested before recording-level artwork existed,
    or whose folder gained a cover afterward, never got its folder or
    embedded art without a full re-ingest.

    Always stamps `images_checked_at`, whether or not anything was found,
    same "always set the status column" contract as _handle_mb_release --
    otherwise enqueue_followups()'s null-check re-queues it forever.
    Skipped (checked_at still stamped) when the recording's folder is
    missing entirely: nothing to scan, and retrying it every boot would
    just repeat the same no-op.
    """
    from datetime import datetime, timezone
    from app.models.recording import Recording
    from app.utils.recording_images import ingest_recording_images

    rec = db.session.get(Recording, recording_id)
    if not rec or rec.images_checked_at is not None:
        return
    library_root = current_app.config.get("LIBRARY_ROOT", "")
    folder_abs = os.path.join(str(library_root), rec.folder_path or "")
    if rec.folder_path and os.path.isdir(folder_abs):
        ingest_recording_images(rec, library_root)
    rec.images_checked_at = datetime.now(timezone.utc)
    # Release already matched before this scan ran: fetch the cover now if
    # the folder had none (2026-10-01). No-op without a release or with art.
    if rec.mb_release_id:
        from app.utils.recording_images import add_cover_art_if_missing
        add_cover_art_if_missing(rec)
    db.session.commit()


_HANDLERS = {
    "audio":      _handle_audio,
    "mb_artist":  _handle_mb_artist,
    "mb_release": _handle_mb_release,
    "images":     _handle_images,
}


def _handle_item(kind, item_id):
    """
    One follow-up item, dispatched by kind. Factored out of the worker loop
    so tests can drain the queue synchronously with no thread involved.

    Each kind's own failure is logged and swallowed here -- one bad recording
    or artist must never take the worker thread down. A legacy kind
    ("analysis"/"score"/"signals") runs through the "audio" handler.
    """
    import traceback as _tb
    handler = _HANDLERS.get(_normalize_kind(kind))
    if handler is None:
        print(f"[ingest] unknown follow-up kind {kind!r}", flush=True)
        return
    try:
        handler(item_id)
    except Exception:
        _tb.print_exc()


def _followup_worker(app, lane_name):
    """Drain one lane forever, one item at a time."""
    lane = _LANES[lane_name]
    while True:
        kind, item_id = lane.get()
        with _Q_LOCK:
            _QUEUED_KEYS.discard((kind, item_id))
            _PENDING_BY_KIND[kind] = max(0, _PENDING_BY_KIND.get(kind, 0) - 1)
        with app.app_context():
            _handle_item(kind, item_id)


def enqueue_followups():
    """
    Queue every follow-up that the current DB state says is still owed, for
    every ingest path alike:

      audio       every LIVE recording with no Listening Quality row OR no
                  track carrying a non_music_score. Never a studio recording.
                  Re-derived from state rather than "everything": a library
                  already scored and signalled is not re-analysed at boot.
      mb_artist   every artist whose MusicBrainz status is "never looked up",
                  plus every matched/linked artist with no genre whose genres
                  were never fetched (owes_artist_genre()).
      mb_release  every STUDIO recording whose release status is "never
                  looked up". Never a live recording.
      images      every recording (any kind) with no recording_image rows and
                  images_checked_at still null.

    Deduped against what is already queued, so calling this twice enqueues
    nothing new. Each item is tagged with its Bulk Ingest run (via
    BulkIngestItem.recording_id) so small runs are served first.

    Returns the counts newly enqueued per kind.
    """
    from sqlalchemy import or_
    from app.models.recording import Recording
    from app.models.recording_image import RecordingImage
    from app.models.artist import Artist
    from app.models.quality import RecordingQuality
    from app.models.track_analysis import TrackAnalysis
    from app.models.track import Track as _Track
    from app.models.bulk_ingest import BulkIngestItem

    app = current_app._get_current_object()

    # recording id / artist id -> newest run it belongs to (absent = no run).
    run_of_rec = dict(db.session.query(BulkIngestItem.recording_id,
                                       func.max(BulkIngestItem.run_id))
                      .filter(BulkIngestItem.recording_id.isnot(None))
                      .group_by(BulkIngestItem.recording_id).all())
    run_of_artist = dict(db.session.query(Performance.artist_id,
                                          func.max(BulkIngestItem.run_id))
                         .join(Recording, Recording.performance_id == Performance.id)
                         .join(BulkIngestItem, BulkIngestItem.recording_id == Recording.id)
                         .group_by(Performance.artist_id).all())

    signalled_recording_ids = (
        db.session.query(_Track.recording_id)
        .join(TrackAnalysis, TrackAnalysis.track_id == _Track.id)
        .filter(TrackAnalysis.non_music_score.isnot(None))
        .distinct()
    )
    audio_ids = [rid for (rid,) in (
        db.session.query(Recording.id)
        .outerjoin(RecordingQuality, RecordingQuality.recording_id == Recording.id)
        .filter(Recording.kind == "live",
                or_(RecordingQuality.id.is_(None),
                    ~Recording.id.in_(signalled_recording_ids)))
        .order_by(Recording.id).all())]
    audio_n = sum(1 for rid in audio_ids
                  if _enqueue(app, "audio", rid, run_of_rec.get(rid)))

    owes_genre = (Artist.mb_status.in_(("matched", "linked"))
                  & Artist.genre_id.is_(None) & Artist.mbid.isnot(None)
                  & func.json_extract(func.coalesce(Artist.mb_extra_json, "{}"),
                                      "$.genres_checked").is_(None))
    artist_ids = [aid for (aid,) in (
        db.session.query(Artist.id).filter(or_(Artist.mb_status.is_(None), owes_genre))
        .order_by(Artist.id).all())]
    mb_n = sum(1 for aid in artist_ids
               if _enqueue(app, "mb_artist", aid, run_of_artist.get(aid)))

    release_ids = [rid for (rid,) in (
        db.session.query(Recording.id)
        .filter(Recording.kind == "studio", Recording.mb_release_status.is_(None))
        .order_by(Recording.id).all())]
    mb_release_n = sum(1 for rid in release_ids
                       if _enqueue(app, "mb_release", rid, run_of_rec.get(rid)))

    image_ids = [rid for (rid,) in (
        db.session.query(Recording.id)
        .outerjoin(RecordingImage, RecordingImage.recording_id == Recording.id)
        .filter(RecordingImage.id.is_(None), Recording.images_checked_at.is_(None))
        .order_by(Recording.id).all())]
    images_n = sum(1 for rid in image_ids
                   if _enqueue(app, "images", rid, run_of_rec.get(rid)))

    return {"audio": audio_n, "mb_artist": mb_n, "mb_release": mb_release_n,
            "images": images_n}


def analysis_snapshot():
    """Queue state for the debug pane. Never raises — it is a debug surface."""
    cur = _ANALYSIS_STATE.get("current")
    return {
        "pending": sum(l.qsize() for l in _LANES.values()),
        "pending_by_kind": dict(_PENDING_BY_KIND),
        "current": (dict(cur, elapsed=round(time.time() - cur["started"], 1))
                    if cur else None),
        "done":    _ANALYSIS_STATE["done"],
        "failed":  _ANALYSIS_STATE["failed"],
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
        # Follow-ups (audio pass, MusicBrainz, artwork) for the new recording
        # go to the follow-up lanes once it exists -- decoupled from this job
        # so they can't hold up reporting "done".
        # The bulk_ingest_followup wrapper, not enqueue_followups() directly: it
        # also reconciles a run's ready/review item that this confirm just
        # ingested (the import page's per-item Review), so the row leaves the
        # Queue instead of still offering Ingest for a folder already imported.
        if (job.get("result") or {}).get("recording_id"):
            with app.app_context():
                from app.utils.bulk_ingest_followup import enqueue_followups as _followups
                _followups()
    except Exception as e:  # noqa: BLE001
        _tb.print_exc()
        job["error"]  = str(e)
        job["status"] = "error"


def _apply_artist_genre(artist, data):
    """
    Set the act's genre from an ingest payload. Does not commit.

    Genre lives on the ACT, not the recording — an act's genre is the same on
    every night it played, which is why there is no genre column on Recording
    and should not be. So the Add Recording form's Genre field writes through
    to the Artist row, and this is where that happens.

    ⚠ This is the only place outside api/genres.py that can CREATE a Genre, and
    it exists because Ryan asked for it (2026-09-01). The Genre design spec's
    rule — nothing creates a genre implicitly — is kept where it was actually
    protecting something: the client only sends `genre_name` when the user
    clicked an explicit "+ Create genre: …" row in the dropdown, never for text
    merely typed into the box. A near-miss spelling cannot mint a second
    vocabulary entry by accident, which is the placeholder-venue contamination
    story in another costume.

    Four guards, because a background ingest job is a bad place to find out an
    assumption was wrong:

      - `genre_id` wins over `genre_name`. An id names a row that exists; a
        name is a request to find or make one.
      - a name is matched case-insensitively against existing rows FIRST, so
        "bluegrass" links Bluegrass rather than creating a twin.
      - an unknown id is IGNORED, not fatal. A stale id from a genre deleted in
        another tab must not fail an ingest that is otherwise fine.
      - ⚠ SILENCE IS NOT A CLEAR. An existing act's genre is only overwritten
        when the payload actually carries one. Batch Import and Auto-Ingest
        never visit the review form and send neither key; treating that
        omission as "clear it" would strip the genre off every act they touch.
        Exactly the None-vs-[] trap the members/guests payload documents below,
        and the reason there is no `else: artist.genre_id = None` here.
    """
    genre_id_in   = data.get("genre_id")
    genre_name_in = (data.get("genre_name") or "").strip()

    genre = None
    if genre_id_in:
        genre = db.session.get(Genre, int(genre_id_in))
    elif genre_name_in:
        genre = db.session.query(Genre).filter(
            func.lower(Genre.name) == genre_name_in.lower()).first()
        if not genre:
            genre = Genre(name=genre_name_in)
            db.session.add(genre)
            db.session.flush()

    if genre and artist.genre_id != genre.id:
        artist.genre_id = genre.id


def _confirm_payload_from_resolved(resolved, scan):
    """
    Build the body _do_confirm() expects, straight out of a Resolved --
    replaces bulk_ingest.confirm_payload() (spec section 4). Whether this
    came from Bulk Ingest or the auto-confirm endpoint, the payload -- and
    therefore the ingested recording -- is identical.
    """
    date = resolved.date.value or {}
    return {
        "source_folder_path": scan.get("folder_path"),
        "artist_name":        resolved.artist.value,
        "start_year":         date.get("year"),
        "start_month":        date.get("month"),
        "start_day":          date.get("day"),
        "venue_name":         resolved.venue.value,
        "event_name":         resolved.event.value,
        "event_create":       False,      # unattended: link to an existing Event, never create one
        "stage":              resolved.stage.value,
        "city":               resolved.city.value,
        "state":              resolved.state.value,
        "country":            resolved.country.value,
        "source":             resolved.source.value,
        "lineage":            resolved.lineage.value,
        "source_tag":         resolved.source_tag.value,
        "etree_shnid":        resolved.shnid.value,
        "info_file_content":  scan.get("info_file_content"),
        "fingerprints":       scan.get("fingerprints"),
        "is_complete":        True,
        "tracks":             resolved.tracks,
        "title":              resolved.album.value,
        "kind":               resolved.kind,
    }


def resolver_json_for_storage(resolver_result):
    """
    The one place a resolver result becomes the string stored in
    recording.resolver_json. Takes Resolved.to_dict() (auto_confirm) or the
    scan's already-serialised `resolved` dict (Add Recording), and drops
    `tracks` -- large, and the recording's own tracks are the truth. Returns
    None for anything that is not a non-empty dict.
    """
    if not isinstance(resolver_result, dict) or not resolver_result:
        return None
    return json.dumps({k: v for k, v in resolver_result.items() if k != "tracks"})


# Blanket ("applies to every recording below") keys -> where they land.
_BLANKET_FIELDS = ("artist", "venue", "event", "stage", "city", "state", "country",
                   "source", "source_tag", "lineage")


def apply_blanket_values(resolved, payload, applied):
    """
    Overwrite the scan's inference with staged blanket values (Review & Ingest
    precedence: a value the person typed beats whatever the tags or info file
    said). Resolved fields are replaced before the verdict so a staged artist
    clears needs_artist; notes and ids have no Resolved field and go straight
    onto the confirm payload (payload may be None before it exists).
    """
    if not applied:
        return
    for key in _BLANKET_FIELDS:
        v = applied.get(key)
        if v:
            setattr(resolved, key, ResolvedField(value=v, source="applied",
                                                  candidates={}, conflict=False))
    if payload is None:
        return
    if applied.get("artist"):
        payload["artist_name"] = applied["artist"]
    for key, dest in (("city", "city"), ("state", "state"), ("country", "country"),
                      ("source", "source"), ("source_tag", "source_tag"),
                      ("lineage", "lineage")):
        if applied.get(key):
            payload[dest] = applied[key]
    if applied.get("venue"):
        # Id and name must move together, or the server files the show under
        # the inferred venue's id while the name says something else.
        payload["venue_name"] = applied["venue"]
        payload["venue_id"] = applied.get("venue_id") or None
    if applied.get("event"):
        payload["event_name"] = applied["event"]
        payload["event_id"] = None
        payload["event_create"] = True        # a person typed it
    if applied.get("stage"):
        payload["stage"] = applied["stage"]
    if applied.get("notes"):
        payload["notes"] = applied["notes"]


def auto_confirm(folder_abs, user_id, *, hash_cache=None,
                  progress_cb=None, cancel_cb=None, phase_cb=None,
                  hold=False, force=False, applied=None):
    """
    The one server function that turns a folder into an ingested (or
    reviewed, or skipped) recording via the resolver -- spec section 4.
    Bulk Ingest's worker and POST /api/ingest/auto-confirm (Batch Import)
    both call this, so an auto-ingest is identical whichever door it came
    through.

    progress_cb/cancel_cb/phase_cb are passed straight through to _do_confirm
    (same signature as /api/ingest/confirm's background job) -- only used
    when status ends up "ingested", since that's the only outcome that
    copies/moves any files. Bulk Ingest's worker (its own direct call) never
    passes these; it is already on its own thread with nothing polling it.

    Returns a dict:
      status       "ingested" | "review" | "skipped" | "failed"
      reasons      list of reason codes (see app.utils.resolve.verdict)
      format       folder_format() string, or None
      detail       human-readable detail for a "failed" status
      resolved     the Resolved this folder produced, or None (no_audio /
                   all files unreadable -- nothing to resolve)
      result       _do_confirm()'s result dict, only when status=="ingested"
      duplicate_of a recording_id, only when status=="skipped"

    hold=True (Review First) stops after the verdict: a clean folder comes back
    status "ready" with nothing copied, moved or written. force=True (the
    person chose "Ingest anyway") ingests a folder the verdict flagged for
    review, but still refuses what cannot be ingested at all: no artist (it
    stays "review") and an exact content duplicate (stays "skipped").
    applied is the run's blanket values (see apply_blanket_values).

    The has_audio / unsupported-format / all-unreadable gate that used to
    live in bulk_ingest.extract() runs here first, same as before -- a
    folder that fails it is never handed to the resolver at all.
    """
    from pathlib import Path as _Path

    library_root = current_app.config.get("LIBRARY_ROOT", "")
    placement = node_settings.get_file_handling().get("placement")

    scan_raw = scan_folder(folder_abs)
    readable_files = [f for f in scan_raw["audio_files"]
                      if _Path(f["filename"]).suffix.lower() in _READABLE_AUDIO_EXTS]
    any_audio = bool(scan_raw["audio_files"]) or bool(scan_raw["unsupported_audio"])
    fmt = folder_format(scan_raw)

    if not any_audio:
        return {"status": "failed", "reasons": ["no_audio"], "format": fmt,
                "detail": "no_audio", "resolved": None, "result": None,
                "duplicate_of": None}
    # WAV/AIFF/SHN/APE/WV are never imported, alone or mixed with FLAC, and
    # not even by force: the person converts the folder first.
    blocked = any(_Path(f["filename"]).suffix.lower() in CONVERTIBLE_AUDIO_EXTS
                  for f in scan_raw["unsupported_audio"])
    if not readable_files or blocked:
        return {"status": "review", "reasons": ["unsupported_format"], "format": fmt,
                "detail": None, "resolved": None, "result": None, "duplicate_of": None}

    scan = build_scan_payload(folder_abs)
    if scan is None:
        return {"status": "failed", "reasons": ["no_audio"], "format": fmt,
                "detail": "no_audio", "resolved": None, "result": None,
                "duplicate_of": None}

    from_tags = scan["suggestions"]["from_tags"]
    readable_tags = [t for t in from_tags["tracks"]
                      if _Path(t.get("filename") or "").suffix.lower() in _READABLE_AUDIO_EXTS]
    unopenable = [t for t in readable_tags if t.get("raw") is None]
    if readable_tags and len(unopenable) == len(readable_tags):
        detail = next((t.get("open_error") for t in unopenable if t.get("open_error")), None)
        return {"status": "failed", "reasons": ["unreadable"], "format": fmt,
                "detail": detail or "unreadable", "resolved": None, "result": None,
                "duplicate_of": None}

    resolved = resolve(scan, library_root=library_root, placement=placement)
    apply_blanket_values(resolved, None, applied)
    resolved.duplicates = find_duplicates(resolved, library_root=library_root,
                                          hash_cache=hash_cache)
    status, reasons = verdict(resolved)
    resolved.status, resolved.reasons = status, reasons

    if status == "skipped":
        dup = next((d for d in resolved.duplicates if d.kind == "content"), None)
        return {"status": "skipped", "reasons": reasons, "format": fmt, "detail": None,
                "resolved": resolved, "result": None,
                "duplicate_of": dup.recording_id if dup else None}

    if status == "review" and not (force and "needs_artist" not in reasons):
        return {"status": "review", "reasons": reasons, "format": fmt, "detail": None,
                "resolved": resolved, "result": None, "duplicate_of": None}

    if hold:
        return {"status": "ready", "reasons": reasons, "format": fmt, "detail": None,
                "resolved": resolved, "result": None, "duplicate_of": None}

    payload = _confirm_payload_from_resolved(resolved, scan)
    # No person confirmed these values, so nothing is learned from the save (aliases,
    # queue re-check). "Ingest anyway" is a person's decision to take them as read.
    payload["unattended"] = not force
    apply_blanket_values(resolved, payload, applied)
    payload["resolver_result"] = resolved.to_dict()
    result = _do_confirm(payload, user_id, progress_cb, cancel_cb=cancel_cb,
                         phase_cb=phase_cb)
    return {"status": "ingested", "reasons": reasons, "format": fmt, "detail": None,
            "resolved": resolved, "result": result, "duplicate_of": None}


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
    from app.utils.download_queue import downloading_here
    if downloading_here(source_folder):
        return jsonify({"error": "This folder is still downloading."}), 409
    if not artist_name:
        return jsonify({"error": "artist_name is required"}), 400
    # R3 (review, 2026-09-25): the Add Recording shnid field is free text.
    # etree_shnid used to be coerced to int only when building Recording()
    # in step 7 of _do_confirm, AFTER step 6 had already moved the source
    # into the library -- a stray non-digit character there failed the job
    # post-move, with the files relocated and no row to show for it. Reject
    # here, before the background job (and its file move) ever starts.
    raw_shnid = data.get("etree_shnid")
    if raw_shnid not in (None, ""):
        try:
            data["etree_shnid"] = int(str(raw_shnid).strip())
        except (TypeError, ValueError):
            return jsonify({"error": f"shnid must be a number: {raw_shnid!r}"}), 400
    else:
        data["etree_shnid"] = None
    job_id = uuid.uuid4().hex
    _INGEST_JOBS[job_id] = {
        "status": "running", "copied": 0, "total": 0,
        "phase": "resolving", "phase_label": PHASES["resolving"],
        "started": time.time(), "phase_at": time.time(),
        "folder": os.path.basename(source_folder.rstrip("/")),
        "artist": artist_name,
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


def _find_event(name):
    """The existing Event a name refers to: the exact name (any case), else the one whose
    normalised key matches ("Telluride BG Festival" and "30th Telluride Bluegrass
    Festival" both find "Telluride Bluegrass Festival"). Lowest id wins a tie."""
    exact = db.session.query(Event).filter(func.lower(Event.name) == name.lower()).order_by(Event.id).first()
    if exact:
        return exact
    key = event_key(name)
    if not key:
        return None
    for eid, ename in db.session.query(Event.id, Event.name).order_by(Event.id):
        if event_key(ename) == key:
            return db.session.get(Event, eid)
    return None


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
      "stage":              "Harbor Stage",   # optional — Performance.stage (new performances)
      "resolver_result":    {...},  # optional -- the scan's `resolved` dict, stored
                                     # (minus tracks) on recording.resolver_json
      "ai_result":          {...},  # optional — raw AI Assist result if run pre-confirm,
                                     # saved as-is to ai_research_json (see run_ai_assist)
      "fingerprints":       [{"type":"ffp","filename":"...","content":"..."}],
      "tracks": [
        {"track_number":1,"title":"Dark Star","set_number":"Set 1","duration":1200,"filename":"t01.flac"}
      ]
    }

    MusicBrainz and the non-music signal never run inline, bulk or not: the
    new artist is created with lookup=False and everything audio/network is a
    follow-up (enqueue_followups(): "audio", "mb_artist", "mb_release",
    "images").
    The tag write (step 12) follows write_tags_on_ingest for a source brought
    in from outside, bulk or not, and is skipped whenever the source is in-root
    (R2-N7) -- since an in-root source's files are the collector's own,
    wherever the confirm call came from.

    MD5 checksum verification (a full re-hash per file) is NEVER run here,
    bulk or interactive alike (Ryan, 2026-09-27: "MD5 never automatic" --
    it runs only from the manual Re-validate / verify-checksums action on
    View Recording). FFP/ST5 header verification still runs at confirm, and
    every fingerprint file is still parsed, matched and stored regardless
    of type; only the MD5 recompute-and-compare step is skipped, leaving
    those entries `unverified`.

    Everything else -- the move, the Recording/Track rows, tag reads --
    is identical to an interactive confirm.
    """
    # No-op when nobody is listening, so every call site below stays unguarded.
    def _phase(key, detail=None):
        if phase_cb:
            phase_cb(key, detail)

    # R3 (review, 2026-09-25): validated again here, before ANYTHING else in
    # this function (including step 6's move), for a direct caller that
    # skips the /confirm route's own check above (tests, or a future
    # caller). A stray non-digit in the free-text shnid field must never
    # surface only after the source folder is already gone.
    _raw_shnid = data.get("etree_shnid")
    if _raw_shnid not in (None, ""):
        try:
            data["etree_shnid"] = int(str(_raw_shnid).strip())
        except (TypeError, ValueError):
            raise ValueError(f"shnid must be a number: {_raw_shnid!r}")
    else:
        data["etree_shnid"] = None

    source_folder = (data.get("source_folder_path") or "").strip()
    artist_name   = (data.get("artist_name")        or "").strip()

    _phase("resolving", artist_name or None)

    city        = (data.get("city")       or "").strip() or None
    state       = (data.get("state")      or "").strip() or None
    country     = (data.get("country")    or "").strip() or None
    venue_name  = (data.get("venue_name") or "").strip() or None
    event_name  = (data.get("event_name") or "").strip() or None
    stage       = (data.get("stage") or "").strip() or None
    start_year  = data.get("start_year")
    start_month = data.get("start_month")
    start_day   = data.get("start_day")
    end_year    = data.get("end_year")
    end_month   = data.get("end_month")
    end_day     = data.get("end_day")

    # title/kind (Bulk Ingest chunk 5): Recording has carried both columns
    # since before this function existed. kind defaults to "live" and is
    # validated against the column's two known values so a bad payload fails
    # loudly here rather than writing a third, silently-unrecognised kind.
    # Resolved this early (rather than just before Recording() below) because
    # step 4's Performance lookup also needs to know it (B1, immediately
    # below).
    rec_kind = data.get("kind") or "live"
    if rec_kind not in ("live", "studio"):
        raise ValueError(f"kind must be 'live' or 'studio': {rec_kind!r}")

    # ── 1. Find or create Artist (the act) ─────────────────────────────────
    # `members`/`guests` are the Add Recording form's two personnel rows — see
    # app/utils/personnel.py::sync_performance_personnel for what they mean at
    # the PERFORMANCE level. Historically this block also used member_names to
    # seed/overwrite the act's ROSTER via set_artist_members, unconditionally,
    # for an existing Artist too — that's the same act-roster-corruption bug
    # Phase 1 already fixed for the recording page's PUT endpoint, just never
    # ported to ingest (flagged as an open gap in the design doc's ripple list,
    # item 5). resolve_or_create_artist(name, member_names) already has the
    # correct behavior baked in — it only seeds member_names as the roster when
    # the Artist is BRAND NEW, and leaves an existing act's roster alone —
    # so passing member_names straight through here fixes it with no new code.
    # Two different needs for the same payload key, so two variables:
    #  - member_names/guest_names (never None) for resolve_or_create_artist,
    #    which just wants "what to seed a BRAND NEW artist's roster with,
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
    #    Members row came out blank even though the artist's own roster
    #    was intact (Ryan, 2026-07-23 bug report — Bela Fleck & Tony
    #    Trischka). Only the manual Add Recording/Batch Review form pre-fills
    #    and always sends both keys (even an intentionally-emptied one), so
    #    that path's behavior is unchanged by this fix.
    member_names_sync = data.get("members")
    guest_names_sync  = data.get("guests")
    member_names = member_names_sync or []
    guest_names  = guest_names_sync  or []
    artist = resolve_or_create_artist(artist_name, member_names, lookup=False)
    if not data.get("unattended"):
        mark_artist_confirmed(artist)       # a person's save (wizard, review, "Ingest anyway") confirms the act

    # ── 2. Genre, on the ARTIST (2026-09-01) ───────────────────────────────
    _apply_artist_genre(artist, data)

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
        event = _find_event(event_name)
        # A person reviewed the wizard form, so it may create. An unattended ingest
        # (auto_confirm) only links to an Event that exists until chunk 6 gives the
        # reader a confidence to gate creation on.
        if not event and data.get("event_create", True) and clean_event_name(event_name):
            event = Event(
                name    = clean_event_name(event_name),
                city    = city    or None,
                state   = state   or None,
                country = country or None,
            )
            db.session.add(event)
            db.session.flush()

    # ── 3.6. Learned aliases (Resolver v2, chunk 8) ──────────────────────────
    # A person's save that differs from what the resolver read teaches the library
    # the text it quoted. Written in this transaction; never fails the save.
    from app.utils.aliases import learn_aliases
    learn_aliases(data, artist, venue)

    # ── 4. Find or create Performance ─────────────────────────────────────────
    # A studio recording always gets its OWN Performance (Ryan, 2026-09-27):
    # never reuse one found by artist/date, unlike a live show, where sharing
    # a Performance across recordings of the same night is the whole point.
    # Reusing one here is exactly how B1 happened -- two studio albums by one
    # artist tagged the same year, no month, share a Performance whose date
    # fields then get filled from whichever album's release lookup runs
    # first, so the second album silently inherits the first one's release
    # day. scripts/migrate_studio_records.py splits apart any Performance an
    # earlier build already shared this way.
    performance = None
    if rec_kind != "studio":
        perf_q = db.session.query(Performance).filter(
            Performance.artist_id == artist.id,
            Performance.start_year  == start_year,
            Performance.start_month == start_month,
            Performance.start_day   == start_day,
        )
        if venue:
            perf_q = perf_q.filter(Performance.venue_id == venue.id)
        performance = perf_q.first()

    if not performance:
        performance = Performance(
            artist_id = artist.id,
            venue_id     = venue.id  if venue  else None,
            event_id     = event.id  if event  else None,
            stage        = stage,
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
            personnel_mode = artist.default_personnel_mode,
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
    # rename_folders governs this (spec section 1.1): off keeps the source
    # folder's own name (still deduped like any other by move_to_library's
    # unique_folder_name()); on names it from the recording's own details.
    file_handling = node_settings.get_file_handling()
    if file_handling["rename_folders"]:
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
    else:
        folder_name = os.path.basename(source_folder.rstrip("/\\")) or "Untitled"

    # ── 6. Move folder into library ─────────────────────────────────────────
    # Copy behavior removed 2026-09-25 (spec section 3.3): a source outside
    # LIBRARY_ROOT is always moved. Move makes re-ingesting the same files
    # structurally impossible, and the source is still recoverable from the
    # library itself.
    library_root = str(current_app.config["LIBRARY_ROOT"])
    # Library LAYOUT is an install-level setting, read here rather than inside
    # move_to_library(), which has no app context (2026-09-17). Off means the
    # show lands flat at the library root -- for a collector who pointed
    # Trellis at a library they built themselves.
    under_artist = node_settings.file_under_artist_folder()

    # Bulk Ingest spec section 1.1, chunk 1 (2026-09-26): a source whose
    # realpath is already inside LIBRARY_ROOT is ingested exactly as it sits
    # on disk -- never moved, renamed, flattened or deduped, in either
    # file_handling mode or placement. move_to_library() enforces the
    # move/rename/dedupe half of that; this flag makes the Track.file_path
    # computation below match it, forcing the identity map + no-flatten
    # regardless of rename_files/naming_scheme.
    try:
        in_root_source = Path(source_folder).resolve().is_relative_to(
            Path(library_root).resolve())
    except OSError:
        in_root_source = False

    tracks_in = data.get("tracks", [])
    # rename_files governs both the filename scheme and whether a multi-
    # disc/-set source flattens into the recording folder's root (spec
    # section 1.1: off means "names and nesting preserved"). Off: every file
    # keeps its own basename via the engine's identity ('original') preset.
    # On: the active naming_scheme decides both the names AND (through
    # flattens()) whether the source's disc/set nesting collapses — the same
    # rule the Rename Files action and the Settings preview use. Map is keyed
    # by each track's ORIGINAL rel_path/filename as scanned; move_to_library()
    # applies it while moving. Fingerprint files (step 9) list the ORIGINAL
    # names too, so matching happens against the original name and only the
    # final DB/verification path uses the new one.
    naming_scheme   = file_handling["naming_scheme"]
    naming_template = file_handling["naming_template"] or None
    if in_root_source:
        audio_rename_map = compute_audio_rename_map(tracks_in, "original")
        flatten = False
    elif file_handling["rename_files"]:
        audio_rename_map = compute_audio_rename_map(
            tracks_in, naming_scheme, naming_template,
            performance = performance,
            source      = data.get("source"),
            source_tag  = data.get("source_tag"),
            etree_shnid = data.get("etree_shnid"),
        )
        flatten = flattens(naming_scheme, naming_template)
    else:
        audio_rename_map = compute_audio_rename_map(tracks_in, "original")
        flatten = False

    # The long one.
    _phase("moving", folder_name)

    try:
        new_folder_path = move_to_library(
            source_folder    = source_folder,
            library_root     = library_root,
            artist_name      = artist_name,
            folder_name      = folder_name,
            progress_cb      = progress_cb,
            audio_rename_map = audio_rename_map,
            cancel_cb        = cancel_cb,
            under_artist_folder = under_artist,
            flatten          = flatten,
        )
    except IngestCancelled:
        # A cancel is not a failure. move_to_library has already undone its own
        # filesystem work; re-raise unchanged so the job reports "cancelled"
        # rather than a scary "File operation failed".
        db.session.rollback()
        raise
    except Exception as e:
        db.session.rollback()
        # An OS-level failure of the move gets its own type so Bulk Ingest can
        # park the folder for review; it is still a RuntimeError for every
        # other caller.
        if isinstance(e, OSError):
            raise MoveFailed(f"File operation failed: {e}")
        raise RuntimeError(f"File operation failed: {e}")

    # move_to_library() may have deduped folder_name against a same-named
    # folder already on disk (see unique_folder_name()) — resync so the
    # "cataloging" phase message and the final response report the name
    # actually written, not the pre-dedup one computed in step 5.
    folder_name = os.path.basename(new_folder_path)

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
    # etree_shnid was already validated/coerced to int-or-None at the top of
    # this function (R3, review 2026-09-25) -- reads straight through here.
    # rec_kind was resolved and validated earlier (step 4 needs it too).
    rec = Recording(
        performance_id       = performance.id,
        source               = data.get("source"),
        source_tag           = (data.get("source_tag") or None),
        etree_shnid          = data.get("etree_shnid"),
        lineage              = data.get("lineage"),
        quality              = data.get("quality"),
        is_complete          = data.get("is_complete", True),
        is_official          = rec_is_official,
        folder_path          = new_folder_path,
        original_folder_name = os.path.basename(source_folder),
        info_file_content    = data.get("info_file_content"),
        notes                = data.get("notes"),
        title                = data.get("title") or None,
        kind                 = rec_kind,
        ai_research_json     = json.dumps(ai_result) if ai_result else None,
        resolver_json        = resolver_json_for_storage(data.get("resolver_result")),
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
        new_filename   = resolve_ingest_file_path(orig_filename, audio_rename_map, flatten)
        track = Track(
            recording_id       = rec.id,
            track_number       = t.get("track_number"),
            title              = t.get("title") or f"Track {t.get('track_number', '?')}",
            set_number         = t.get("set_number") or None,
            disc_number        = t.get("disc_number"),
            disc_track_number  = t.get("disc_track_number"),
            original_file_path = orig_filename or None,
            duration           = t.get("duration"),
            file_path          = new_filename,
            is_official        = track_official,
            flags              = json.dumps(flags_raw) if flags_raw else None,
            songwriter         = t.get("songwriter") or None,
            notes              = t.get("notes") or None,
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
            content = fp.get("content")
        db.session.add(RecordingFingerprint(
            recording_id     = rec.id,
            fingerprint_type = fp_type,
            filename         = fp.get("filename"),
            content          = content,
            rel_path         = rel_path,
        ))
        if content and created_tracks:
            # A fingerprint file nested inside a disc/set subdir (e.g.
            # "CD1/checksum.md5") almost always lists filenames scoped to
            # that disc only ("01.flac", "02.flac", ...). When the active
            # naming scheme flattens audio, those bare names can collide
            # across discs —
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
                ChecksumMatchProxy(track, original_filenames.get(track, track.file_path))
                for track in candidates
            ]
            matches = match_entries_to_tracks(parse_checksum_file(content), proxies)
            now = datetime.now(timezone.utc)
            for proxy, expected in matches.items():
                track          = proxy.real
                track_abs_path = os.path.join(library_root, new_folder_path, track.file_path)
                track.checksum_type        = fp_type
                track.expected_checksum    = expected
                # MD5 is NEVER computed automatically at confirm, bulk or
                # interactive alike (Ryan, 2026-09-27: "MD5 never automatic"
                # -- it runs only from the manual Re-validate / verify-
                # checksums action on View Recording). A full re-hash of
                # every byte of every file is minutes rather than seconds
                # across a whole-library pass, and even for one interactive
                # ingest it is unasked-for work the confirm click never
                # promised. FFP/ST5 still verify here -- both only read a
                # FLAC header and cost nothing extra. The fingerprint file
                # itself is still parsed, matched and stored above
                # regardless of type -- only the recompute-and-compare step
                # is conditional.
                if fp_type == "md5":
                    track.checksum_status = "unverified"
                else:
                    track.checksum_status = verify_track_checksum(track_abs_path, fp_type, expected)
                track.checksum_verified_at = now

    # ── 10. Irrevocable ingest event ──────────────────────────────────────────
    db.session.add(RecordingEvent(
        recording_id = rec.id,
        user_id      = user_id,
        event_type   = "ingested",
        note         = f"behavior=move original={os.path.basename(source_folder)}",
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
    # ── 9.5. Non-music signal: NOT here ───────────────────────────────────────
    # It, the Listening Quality score and Full analysis are the "audio"
    # follow-up now, for every ingest path (bulk or not), so the ingest
    # itself stays I/O-only and finishes first.

    _phase("saving")
    # N5: promote_to_recording's own flush can raise (e.g. no admin user for
    # RecordingEvent.user_id) -- that used to be swallowed here and printed,
    # after which the very next line's db.session.commit() raised a SEPARATE,
    # unrelated PendingRollbackError from the dirty session, and THAT is what
    # every caller (including process()'s per-item except) reported as the
    # failure. Let the real exception propagate instead: the caller's own
    # rollback + failure handling is exactly what an ingest error already
    # goes through, so nothing is lost by not catching it a second time here.
    from app.utils.quality_store import promote_to_recording
    promote_to_recording(source_folder, rec.id, commit=False)

    db.session.commit()

    checksum_mismatches = sum(1 for t in created_tracks if t.checksum_status == "mismatch")

    # ── 12. Ingest tag write ────────────────────────────────────────────────
    # write_tags_on_ingest gates this; off by default under 'keep'. Runs
    # after the move, after the rename map, and after the main commit above,
    # so it writes to the tracks' FINAL path with FINAL track_number/title —
    # the same write_flac_tags() the View Recording action uses (spec
    # section 3.2). Per-file errors are reported on the job result, not
    # rolled back; the ingest itself already succeeded. Step 9's stored
    # checksum status is not recomputed.
    tag_errors = []
    # Unified import (2026-10-02): bulk no longer suppresses this. A source
    # brought in from outside is ours to tag (when write_tags_on_ingest);
    # only an in-root source, cataloged in place, is never touched.
    # R2-N7: any in-root source skips the ingest tag write, not only a bulk
    # (bulk=True) bulk_ingest run. in_root_source already means "this folder
    # sits inside the collector's own library, exactly where discover()
    # found it" (spec section 1.1) -- Review & Ingest's Accept action reaches
    # here with bulk=False for an in-root show too (it is a deliberate click,
    # not an unattended pass), but the page it's clicked from says "Your
    # files are not moved or changed," and step 12 writing tags into the
    # collector's own file directly contradicts that. A source arriving from
    # OUTSIDE the root still follows the write_tags_on_ingest switch exactly
    # as before, whether bulk or not.
    if file_handling["write_tags_on_ingest"] and not in_root_source:
        _phase("tags", f"{len(created_tracks)} tracks")
        n_written, tag_errors = write_flac_tags(rec, library_root)
        if n_written > 0:
            note = f"{n_written} file(s) written"
            if tag_errors:
                note += f"; {len(tag_errors)} error(s): " + "; ".join(f[0] for f in tag_errors)
            db.session.add(RecordingEvent(
                recording_id = rec.id,
                user_id      = user_id,
                event_type   = "tags_written",
                note         = note,
            ))
            db.session.commit()

    # ── 13. Recording artwork (Studio Records spec v1, chunk 4) ─────────────
    # After the recording/tracks are committed and after step 12's (skipped
    # or not) tag write, so it reads the tracks' FINAL on-disk names exactly
    # like step 12 does. Runs for both doors -- bulk_ingest_run.process()
    # calls this same _do_confirm(bulk=True), so a step in here covers both
    # without a separate bulk path. Never fatal: a bad or unreadable image
    # must not fail an otherwise-successful ingest, so any exception here is
    # caught and reported on the job result rather than raised. (The helper
    # itself already swallows every per-image error; this is a second, wider
    # net for anything unexpected -- e.g. the folder having vanished.)
    image_errors = []
    _phase("images")
    try:
        from app.utils.recording_images import ingest_recording_images
        ingest_recording_images(rec, library_root)
        db.session.commit()
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        import traceback as _tb4
        _tb4.print_exc()
        image_errors.append(str(e))

    # A person has settled this act, venue and event: the other review rows of the same
    # import run that quote the same reading are looked at again (Resolver v2, chunk 8).
    if not data.get("unattended"):
        try:
            from app.utils.aliases import confirmed_keys
            from app.utils import bulk_ingest_run as _bir
            _bir.recheck_queue(source_folder, confirmed_keys(
                data, artist, venue, event.name if event else event_name))
        except Exception:  # noqa: BLE001 -- never fails a save that already succeeded
            db.session.rollback()
            import traceback as _tb5
            _tb5.print_exc()

    return {
        "recording_id":        rec.id,
        "artist_id":        artist.id,
        "folder_name":         folder_name,
        "event_id":            event.id if event else None,
        "stage":               performance.stage,
        "checksum_mismatches": checksum_mismatches,
        "tag_errors":          tag_errors,
        "image_errors":        image_errors,
    }


import re as _re
from collections import Counter as _Counter

_TEXT_EXTS   = {'.txt', '.nfo', '.md', '.text', '.log'}
_LOSSY_EXTS  = {'.mp3', '.aac', '.ogg', '.m4a'}
_LOSSLESS_EXTS = {'.flac', '.wav', '.aiff', '.aif', '.ape', '.wv'}
# Canonical home is app/utils/ingest.py (FOLDER_DATE_RE) -- Ingest Field
# Resolver spec v1 section 10. Aliased here so _audit_incoming_folder and
# batch_scan's date fallback below keep their existing spelling.
from app.utils.ingest import FOLDER_DATE_RE as _DATE_RE
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
        issues.append({"severity": "warn", "msg": "WAV/AIFF, not FLAC"})
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
                     source, lineage, track_count, tracks_titled, format, kind }
                     format/kind computed the same way bulk_ingest.py's
                     extract()/classify() do (2026-09-27), so this table's
                     FORMAT/TYPE pills match the other two ingest surfaces.
      - paula: compute_paula_score() result, or None (empty/unreadable folder).
               Purple-border source-of-truth in Add Recording's field-level
               confidence highlighting — no longer rendered as its own
               narrative/avatar anywhere in the UI (removed 2026-07-16).
      - already_ingested: bool  (folder path already in DB)
    """
    from app.utils.ingest import build_scan_payload, scan_folder
    from app.models.recording import Recording
    from app.utils.paula import compute_paula_score
    from app.utils.bulk_ingest import folder_format
    from app.utils.resolve import resolve, verdict

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
    known_artists = [p.name for p in db.session.query(Artist.name).all()]
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

        # Field resolution + Format/Type/verdict all come from the resolver
        # now (spec section 4: "batch_scan reads resolved; tier = verdict") --
        # no separate hand-rolled merge here to drift out of sync with Bulk
        # Ingest's or the wizard's.
        folder_format_str = None
        kind = None
        resolved = None
        reasons = []
        status = None
        if scan:
            try:
                folder_format_str = folder_format(scan_folder(folder_path))
            except Exception:
                folder_format_str = None
            try:
                from app.utils import node_settings as _node_settings
                placement = _node_settings.get_file_handling().get("placement")
                resolved = resolve(scan, library_root=current_app.config.get("LIBRARY_ROOT"),
                                   placement=placement)
                kind = resolved.kind
                status, reasons = verdict(resolved)
            except Exception:
                resolved = None

        # Paula's per-item confidence read — same engine as the interactive
        # scan endpoint (app/api/recordings.py). Frontend aggregates these
        # into a single batch-level narrative rather than showing per-row
        # scores (Ryan didn't want per-item Paula numbers cluttering the list).
        paula_result = None
        if scan:
            try:
                paula_result = compute_paula_score(scan, known_artists, known_venues)
            except Exception:
                paula_result = None

        # ── Field resolution: read straight off the resolver's Resolved ───────
        artist = resolved.artist.value if resolved else None
        artist_in_db = bool(
            artist and any(artist.lower() == p.lower() for p in known_artists)
        )
        artist_fuzzy = bool(from_info.get("artist_match"))

        date_val = (resolved.date.value if resolved else None) or {}
        year, month, day = date_val.get("year"), date_val.get("month"), date_val.get("day")

        # Tracks -- resolved.tracks is already the resolver's own merged list
        # (title-cased, flagged, songwriter attached); reshaped here to the
        # {number, title, source} preview shape this endpoint has always sent.
        tag_trks = from_tags.get("tracks", [])
        titled_count = sum(
            1 for t in tag_trks if t.get("title") and t["title"].strip()
        )
        info_tracks  = from_info.get("tracks", [])

        merged_tracks = []
        if resolved:
            for t in resolved.tracks:
                merged_tracks.append({
                    "number": t["track_number"],
                    "title":  t["title"],
                    "source": "tags" if any(
                        (tt.get("rel_path") or tt.get("filename")) == t["filename"]
                        and (tt.get("title") or "").strip()
                        for tt in tag_trks
                    ) else ("info" if info_tracks else None),
                })

        venue   = resolved.venue.value if resolved else None
        city    = resolved.city.value if resolved else None
        state   = resolved.state.value if resolved else None
        country = resolved.country.value if resolved else None
        source  = resolved.source.value if resolved else None
        lineage = resolved.lineage.value if resolved else None

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
        # Tier = the resolver's verdict (spec section 4): "ingested" reads
        # green, "review"/"skipped" yellow, anything else (no scan, or a
        # hard "failed" gate) red. This replaces health["band"] as the tier
        # source -- health is still returned as its own field for the score
        # badge, just no longer doubling as the tier.
        if status == "ingested":
            tier = "green"
        elif status in ("review", "skipped"):
            tier = "yellow"
        else:
            tier = "red"

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
                "format":           folder_format_str,
                "kind":             kind,
                "reasons":          reasons,
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
