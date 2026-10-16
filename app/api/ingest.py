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
from app import lomax
from app.models.lomax import LomaxRun
from app.utils.paths import recording_folder_abs
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


# resolve_similar_artist_ids / _act_key / _ARTIST_SIMILARITY / _ACT_NOISE_WORDS
# moved to app/utils/artists.py. Re-imported here so every existing call site
# in this file (and `from app.api.ingest import resolve_similar_artist_ids`
# elsewhere) keeps working unchanged.
from app.utils.artists import (resolve_similar_artist_ids, _act_key,
                               _ARTIST_SIMILARITY, _ACT_NOISE_WORDS)


@bp.route("/check-existing", methods=["GET"])
@login_required
def check_existing():
    """GET /api/ingest/check-existing?artist_name=...&year=...&month=...&day=...
    Read-only lookup (no creation) — the Add Recording form calls this once
    artist + date are known, to WARN (not block) when the library already has a
    performance for that artist/date. Multiple recordings per performance are
    legitimate (SBD + AUD of the same show), so this never prevents Confirm —
    it just surfaces what's already there so an archivist doesn't accidentally
    re-ingest a tape they already have.

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
    exclude_id = request.args.get("exclude_id", type=int)   # the recording being reviewed never matches itself
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
        # Recordings in Workshop or Backlog count too, named by where they are
        # (`location` is None for the library).
        recs = [r for r in p.recordings if r.id != exclude_id]
        if not recs:
            continue   # nothing against it — no duplicate risk
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
                    "location":    None if r.is_published else (r.location or "library"),
                    "source":      r.source,
                    "quality":     r.quality,
                    "track_count": len(r.tracks),
                    "created_at":  r.created_at.isoformat() if r.created_at else None,
                }
                for r in recs
            ],
        })
    return jsonify({"artist_found": True, "performances": performances})


@bp.route("/similar-acts", methods=["GET"])
@login_required
def similar_acts():
    """
    GET /api/ingest/similar-acts?artist_name=...
    Every act already in the library that plausibly IS the named act (exact, same words once
    noise words go, or a close spelling): the same resolve_similar_artist_ids() the duplicate
    check uses. The Add Recording form asks before it pre-fills Members for a brand-new act.
    """
    name = (request.args.get("artist_name") or "").strip()
    ids = resolve_similar_artist_ids(name) if name else []
    rows = db.session.query(Artist.id, Artist.name).filter(Artist.id.in_(ids)).all() if ids else []
    return jsonify({"acts": [{"id": i, "name": n} for i, n in rows]})


@bp.route("/release-candidates", methods=["GET"])
@login_required
def release_candidates():
    """
    GET /api/ingest/release-candidates?artist=...&title=...&tracks=N
    MusicBrainz release candidates for an album that is not saved yet (Add Recording).
    Searches only when asked and never picks: the person chooses one from the list.
    Writes nothing.
    """
    from app.utils import musicbrainz as mb
    artist = (request.args.get("artist") or "").strip()
    title = (request.args.get("title") or "").strip()
    if not artist or not title:
        return jsonify({"candidates": []})
    if not mb.enabled():
        return jsonify({"error": "MusicBrainz lookups are disabled"}), 503
    try:
        tracks = int(request.args.get("tracks") or 0) or None
    except ValueError:
        tracks = None
    mb.reset_breaker()
    candidates = mb.search_release(artist, title)
    _status, _best, ranked = mb.classify_release(candidates, tracks)
    return jsonify({"candidates": ranked})


@bp.route("/release-detail", methods=["GET"])
@login_required
def release_detail():
    """
    GET /api/ingest/release-detail?mbid=...
    One MusicBrainz release in full (title, label, catalog number, country, date, track titles),
    for the Add Recording form to fill its empty fields from the release the person picked.
    Writes nothing.
    """
    from app.utils import musicbrainz as mb
    mbid = (request.args.get("mbid") or "").strip()
    if not mbid:
        return jsonify({"error": "mbid is required"}), 400
    if not mb.enabled():
        return jsonify({"error": "MusicBrainz lookups are disabled"}), 503
    mb.reset_breaker()
    return jsonify({"release": mb.lookup_release(mbid)})


@bp.route("/genre-suggestion", methods=["GET"])
@login_required
def genre_suggestion():
    """
    GET /api/ingest/genre-suggestion?artist_name=...
    The genre MusicBrainz gives an act that is new, or has no genre, so the Resolver table can
    offer it in the attended flows (Add Recording, import review). An online lookup outside the
    offline resolver, cached, and quietly empty when offline. Writes nothing: the person accepts it
    into the form, and Confirm applies it through the usual genre path (fill-if-empty is the form's
    own rule: an act that already has a genre is never offered one).
    """
    from app.utils import musicbrainz as mb
    name = (request.args.get("artist_name") or "").strip()
    if not name:
        return jsonify({"genre": None})
    artist = db.session.query(Artist).filter(func.lower(Artist.name) == name.lower()).first()
    if artist is not None and artist.genre_id is not None:
        return jsonify({"genre": None, "has_genre": True})
    return jsonify({"genre": mb.suggest_genre(name, getattr(artist, "mbid", None) if artist else None)})


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


_INGEST_JOBS = {}  # job_id -> {status, phase, copied, total, result, error}

# ═════════════════════════════════════════════════════════════════════════════
# Confirm-job phases
# ═════════════════════════════════════════════════════════════════════════════
# The confirm job reported exactly two things — `copied` and `total` — which
# are only meaningful during ONE of its four phases. Everything before the copy
# (resolving the act, venue, event and performance) and everything after it
# (checksum verification, which reads every track when the folder ships .md5)
# showed as a motionless 0/0, so a slow ingest was indistinguishable from a
# hung one.
#
# Labels are user-facing and deliberately name the WORK, not the code.
PHASES = {
    "resolving":  "Reading folder and matching metadata",
    "copying":    "Copying files into the library",
    "moving":     "Moving files into the library",
    "cataloging": "Cataloging tracks",
    "checksums":  "Verifying checksums",
    # Deliberately vague. The phase measures each track to decide whether
    # it looks like music, but naming the heuristic in a progress line
    # tells the user about our internals rather than about their ingest.
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
# Measured on one such track: librosa.load 19.6 s, beat_track 29.1 s, full-track
# STFT + centroid 5.9 s — 57.7 s per track, roughly 16 minutes for that 8-track
# show. Seventeen of those at once is the thing that actually made ingestion
# feel slow; the listening-quality pass everyone suspects costs about 3 seconds
# a show.
#
# A single worker does not make the analysis faster. It makes it FINITE, keeps
# it off the critical path of the next copy, and — because the queue is now a
# real object — makes it reportable, which is what the debug pane needs.
# Follow-up kinds. The queue is in memory (every kind is re-derived from DB
# state by enqueue_followups, so nothing needs to survive a restart).
#   "audio"      ONE audio pass for a recording: Listening Quality score +
#                non-music signal + Full (librosa) analysis. Live only when
#                enqueued automatically; see run_audio_pass().
#   "mb_artist"  a MusicBrainz lookup for one artist still at "never looked
#                up", deferred off the interactive ingest path; or, for a
#                matched artist with no genre, one genre fetch.
#   "mb_release" a MusicBrainz release lookup for one studio recording
#                still at "never looked up" (mb_release_status IS NULL).
#                Never enqueued for a live recording.
#   "images"     artwork scan for a recording never scanned for it.
#   "completeness" is this Album part of a longer release (tags, then
#                MusicBrainz)? Studio only, after its release lookup.
#   "mb_queue"   MusicBrainz release lookup for an Album row waiting in the
#                Queue (a BulkIngestItem id, not a recording): found or not,
#                shown as the row's MusicBrainz icon.
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
_PENDING_BY_KIND = {"audio": 0, "mb_artist": 0, "mb_release": 0, "images": 0, "completeness": 0,
                    "mb_queue": 0}

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
    # Where the folder actually is: library, Workshop or Backlog.
    from app.utils.paths import location_root, recording_folder_abs
    folder_root = location_root(rec.location, library_root)

    # 1. Listening Quality. Same scorer chain as manual triage; unreadable
    # audio returns an {"error"} dict and no row is written, so the recording
    # stays unscored and a later enqueue_followups() retries it.
    if score:
        try:
            from app.utils.quality import (extract_recording_features, score_recording,
                                           guess_source_from_name)
            from app.utils.quality_store import upsert_for_recording
            features = extract_recording_features(os.path.join(folder_root, folder_path))
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
            _store_non_music_signal(rec.tracks, folder_root, folder_path)
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
    # Completeness waits for the release lookup; queue it now it has run.
    if rec.kind == "studio" and rec.completeness_checked_at is None:
        _enqueue(current_app._get_current_object(), "completeness", rec.id)


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
    from app.utils.paths import recording_folder_abs

    rec = db.session.get(Recording, recording_id)
    if not rec or rec.images_checked_at is not None:
        return
    library_root = current_app.config.get("LIBRARY_ROOT", "")
    folder_abs = recording_folder_abs(rec, library_root)
    if folder_abs and os.path.isdir(folder_abs):
        ingest_recording_images(rec, library_root)
    rec.images_checked_at = datetime.now(timezone.utc)
    # Release already matched before this scan ran: fetch the cover now if
    # the folder had none. No-op without a release or with art.
    if rec.mb_release_id:
        from app.utils.recording_images import add_cover_art_if_missing
        add_cover_art_if_missing(rec)
    db.session.commit()


def _handle_completeness(recording_id):
    """Album completeness for one studio recording not yet checked. NULL is
    left in place when MusicBrainz could not be asked, so it is retried."""
    from app.models.recording import Recording
    from app.utils.album_completeness import check_album
    rec = db.session.get(Recording, recording_id)
    if not rec or rec.completeness_checked_at is not None:
        return
    check_album(rec)
    db.session.commit()


def _handle_mb_queue(item_id):
    """Release lookup for one Album row in the Queue (Ryan, 2026-10-13). The
    result rides in the item's meta as {"found", "status", "mbid", "title"};
    "found" is the same confident match the import-time lookup would make.
    Nothing is stored when MusicBrainz cannot be asked, so the row is tried
    again at the next enqueue."""
    import json as _json
    from app.models.bulk_ingest import BulkIngestItem
    from app.utils import musicbrainz as _mb
    it = db.session.get(BulkIngestItem, item_id)
    if it is None or it.kind != "studio" or it.status not in ("ready", "review"):
        return
    try:
        meta = _json.loads(it.meta or "{}") or {}
    except ValueError:
        return
    if meta.get("mb") or not _mb.enabled():
        return
    if _mb.tripped():
        _retry_mb_queue_later(it, _mb.cooldown_left())
        return
    artist, title = meta.get("artist"), meta.get("title")
    status, best = "none", None
    if artist and title:
        before = _mb.failure_count()
        cands = _mb.search_release(artist, title)
        # A failed request is "could not ask", never "not found" (2026-10-13).
        if _mb.tripped() or _mb.failure_count() > before:
            _retry_mb_queue_later(it, _mb.cooldown_left() or 30)
            return
        status, best, _ = _mb.classify_release(cands, meta.get("track_count"))
    meta["mb"] = {"found": status == "matched", "status": status,
                  "mbid": best.get("mbid") if best else None,
                  "title": best.get("title") if best else None}
    it.meta = _json.dumps(meta)
    db.session.commit()


def _retry_mb_queue_later(item, delay):
    """Queue this row's lookup again once MusicBrainz can be asked."""
    app = current_app._get_current_object()
    item_id, run_id = item.id, item.run_id
    t = threading.Timer(max(1.0, delay), lambda: _enqueue(app, "mb_queue", item_id, run_id))
    t.daemon = True
    t.start()


def queue_album_lookup(item):
    """Queue the MusicBrainz lookup for an Album row once it has been read."""
    from app.utils import musicbrainz as _mb
    if not _mb.enabled():                 # off under TESTING, like every lookup
        return
    if item.kind == "studio" and item.status in ("ready", "review"):
        _enqueue(current_app._get_current_object(), "mb_queue", item.id, item.run_id)


_HANDLERS = {
    "mb_queue":   _handle_mb_queue,
    "completeness": _handle_completeness,
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

    Recordings still in the Queue are left out of all of it (and so are acts only they
    know): nothing is analysed or looked up until a person imports one.

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
        .filter(Recording.kind == "live", Recording.location != "queue",
                or_(RecordingQuality.id.is_(None),
                    ~Recording.id.in_(signalled_recording_ids)))
        .order_by(Recording.id).all())]
    audio_n = sum(1 for rid in audio_ids
                  if _enqueue(app, "audio", rid, run_of_rec.get(rid)))

    owes_genre = (Artist.mb_status.in_(("matched", "linked"))
                  & Artist.genre_id.is_(None) & Artist.mbid.isnot(None)
                  & func.json_extract(func.coalesce(Artist.mb_extra_json, "{}"),
                                      "$.genres_checked").is_(None))
    # Not for an act only a Queue recording knows: nothing is looked up, and so nothing
    # becomes "enriched", for a guess nobody has imported or confirmed.
    queue_acts = (db.session.query(Performance.artist_id)
                  .join(Recording, Recording.performance_id == Performance.id)
                  .filter(Recording.location == "queue"))
    off_queue_acts = (db.session.query(Performance.artist_id)
                      .join(Recording, Recording.performance_id == Performance.id)
                      .filter(Recording.location != "queue"))
    artist_ids = [aid for (aid,) in (
        db.session.query(Artist.id).filter(or_(Artist.mb_status.is_(None), owes_genre),
                                           or_(Artist.id.in_(off_queue_acts), Artist.confirmed_at.isnot(None),
                                               ~Artist.id.in_(queue_acts)))
        .order_by(Artist.id).all())]
    mb_n = sum(1 for aid in artist_ids
               if _enqueue(app, "mb_artist", aid, run_of_artist.get(aid)))

    release_ids = [rid for (rid,) in (
        db.session.query(Recording.id)
        .filter(Recording.kind == "studio", Recording.location != "queue",
                Recording.mb_release_status.is_(None))
        .order_by(Recording.id).all())]
    mb_release_n = sum(1 for rid in release_ids
                       if _enqueue(app, "mb_release", rid, run_of_rec.get(rid)))

    # Album rows waiting in the Queue that have not been looked up yet.
    import json as _json
    mb_queue_n = 0
    for iid, meta in (db.session.query(BulkIngestItem.id, BulkIngestItem.meta)
                      .filter(BulkIngestItem.kind == "studio",
                              BulkIngestItem.status.in_(("ready", "review"))).all()):
        try:
            looked = bool((_json.loads(meta or "{}") or {}).get("mb"))
        except ValueError:
            looked = True
        if not looked and _enqueue(app, "mb_queue", iid, None):
            mb_queue_n += 1

    # After the release lookup, so a release it links is the one compared.
    completeness_ids = [rid for (rid,) in (
        db.session.query(Recording.id)
        .filter(Recording.kind == "studio", Recording.location != "queue",
                Recording.mb_release_status.isnot(None),
                Recording.completeness_checked_at.is_(None))
        .order_by(Recording.id).all())]
    completeness_n = sum(1 for rid in completeness_ids
                         if _enqueue(app, "completeness", rid, run_of_rec.get(rid)))

    image_ids = [rid for (rid,) in (
        db.session.query(Recording.id)
        .outerjoin(RecordingImage, RecordingImage.recording_id == Recording.id)
        .filter(RecordingImage.id.is_(None), Recording.images_checked_at.is_(None),
                Recording.location != "queue")
        .order_by(Recording.id).all())]
    images_n = sum(1 for rid in image_ids
                   if _enqueue(app, "images", rid, run_of_rec.get(rid)))

    return {"audio": audio_n, "mb_artist": mb_n, "mb_release": mb_release_n,
            "images": images_n, "completeness": completeness_n, "mb_queue": mb_queue_n}


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
    """Set the act's genre from an ingest payload. Does not commit.

    Genre lives on the ACT, not the recording — an act's genre is the same on
    every night it played, which is why there is no genre column on Recording
    and should not be. So the Add Recording form's Genre field writes through
    to the Artist row, and this is where that happens.

    The Genre rule — nothing creates a genre implicitly — is kept where it was
    actually protecting something: the client only sends `genre_name` when the
    user clicked an explicit "+ Create genre: …" row in the dropdown, never for
    text merely typed into the box. A near-miss spelling cannot mint a second
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
    # Fill-if-empty, unless a person picked this genre by hand in the form
    # (genre_source == "hand"). Suggestions (MusicBrainz, Lomax), unattended
    # imports and a missing flag only ever fill an empty genre.
    if artist.genre_id is not None and data.get("genre_source") != "hand":
        return
    genre_id_in   = data.get("genre_id")
    genre_name_in = (data.get("genre_name") or "").strip()

    genre = None
    if genre_id_in:
        genre = db.session.get(Genre, int(genre_id_in))
    elif genre_name_in:
        genre = db.session.query(Genre).filter(
            func.lower(Genre.name) == genre_name_in.lower()).first()
        if not genre:
            from app.utils.genre_color import next_genre_color
            genre = Genre(name=genre_name_in, color=next_genre_color(db.session))
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
        # Engineer names (live only; _do_confirm ignores them for a studio record).
        "recording_engineer": resolved.recording_engineer.value,
        "transfer_engineer":  resolved.transfer_engineer.value,
        "mastering_engineer": resolved.mastering_engineer.value,
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
                  hold=False, force=False, applied=None, mb_release_id=None, item=None,
                  unpublished_cache=None):
    """
    The one server function that turns a folder into an ingested (or
    reviewed, or skipped) recording via the resolver -- spec section 4.

    Every folder that reads successfully gets its recording row here (location
    'queue', linked to `item` by the caller) before anything else happens to it:
    a Ready or Needs Review row IS a recording. A folder that fails to read gets
    none, and neither does an exact duplicate (a library duplicate is skipped; a
    duplicate of a recording in the Queue, Workshop or Backlog is skipped and
    points at it). A folder that already is a recording (its Queue row's, or one
    in Workshop/Backlog) is read again against that recording: values a person
    saved from the Review form are kept. Import is promote_to_library().
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
    stays "review"), an exact content duplicate (stays "skipped") and a folder
    that is a recording in Workshop/Backlog (stays "review", duplicate_of names it).
    applied is the run's blanket values (see apply_blanket_values).

    The has_audio / unsupported-format / all-unreadable gate that used to
    live in bulk_ingest.extract() runs here first, same as before -- a
    folder that fails it is never handed to the resolver at all.
    """
    from pathlib import Path as _Path

    library_root = current_app.config.get("LIBRARY_ROOT", "")
    placement = node_settings.get_file_handling().get("placement")

    # A folder that is gone (imported or moved since it was queued) says so,
    # rather than reading as a folder without audio (2026-10-13).
    if not os.path.isdir(folder_abs):
        return {"status": "failed", "reasons": ["folder_missing"], "format": None,
                "detail": None, "resolved": None, "result": None, "duplicate_of": None}

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
        # A reader error caused by a truncated file says so (2026-10-13).
        from app.utils.truncated_audio import truncated_files
        short = truncated_files(folder_abs)
        if short:
            return {"status": "failed", "reasons": ["truncated_file"], "format": fmt,
                    "detail": ", ".join(short[:3]) + (f" and {len(short) - 3} more" if len(short) > 3 else ""),
                    "resolved": None, "result": None, "duplicate_of": None}
        return {"status": "failed", "reasons": ["unreadable"], "format": fmt,
                "detail": detail or "unreadable", "resolved": None, "result": None,
                "duplicate_of": None}

    if applied and applied.get("clear_disc_numbers"):
        from app.utils.resolve import clear_disc_numbers
        clear_disc_numbers(scan)
    own = (db.session.get(Recording, item.recording_id) if item is not None and item.recording_id else None)
    if own is None:
        from app.utils.resolve import recording_at_folder
        own = recording_at_folder(folder_abs)
    if own is not None and own.location == "library":
        own = None
    # A recording a person edited (or one that came back from the library) keeps what is saved.
    keep_saved = own is not None and (own.location != "queue" or person_edited(own))
    if own is not None and own.location != "queue":
        hold = True                      # a Workshop/Backlog recording is imported by a person, never by the worker

    resolved = resolve(scan, library_root=library_root, placement=placement)
    if keep_saved:
        _overlay_saved(resolved, own)
    else:
        apply_blanket_values(resolved, None, applied)
    resolved.duplicates = find_duplicates(resolved, library_root=library_root, hash_cache=hash_cache,
                                          own_recording_id=own.id if own is not None else None,
                                          unpublished_cache=unpublished_cache)
    status, reasons = verdict(resolved)
    resolved.status, resolved.reasons = status, reasons

    if status == "skipped":
        dup = next((d for d in resolved.duplicates if d.kind == "content"), None)
        dup = dup or next((d for d in resolved.duplicates if d.kind == "unpublished"), None)
        return {"status": "skipped", "reasons": reasons, "format": fmt, "detail": dup.location if dup else None,
                "resolved": resolved, "result": None, "recording_id": None,
                "duplicate_of": dup.recording_id if dup else None}

    # Albums only: is this folder an incomplete part of a longer official
    # release (a Various Artists box set split up by artist)? A MusicBrainz
    # call, so it runs here and not in the resolver, which stays offline.
    # Under "Ingest anyway" (force) it still runs, so the Album is imported
    # marked incomplete, but it no longer holds the row back.
    # The folder's own tags are checked first (TRACKTOTAL against the files
    # present, offline); MusicBrainz is only asked when they say nothing.
    detail = None
    if resolved.kind == "studio":
        from app.utils.resolve import tag_track_shortfall
        short = tag_track_shortfall(scan)
        if short:
            detail = f"{short['have']} of {short['expected']} {short['unit']}"
        else:
            from app.utils.musicbrainz import find_incomplete_release
            found = find_incomplete_release(resolved.artist.value, resolved.album.value,
                                            [t.get("title") for t in resolved.tracks or []])
            if found:
                detail = f"{found['folder_tracks']} of {found['release_tracks']} tracks"
        if detail:
            reasons = reasons + ["incomplete_release"]
            if not force:
                status = "review"
            resolved.status, resolved.reasons = status, reasons
    elif resolved.kind == "live":
        # A live show gets the same offline tag reading (track and disc
        # numbers, totals); no MusicBrainz, a show is not a release. Measured
        # on the laptop library 2026-10-13: no false flags on 32 live shows.
        from app.utils.resolve import tag_track_shortfall
        short = tag_track_shortfall(scan)
        if short:
            detail = f"{short['have']} of {short['expected']} {short['unit']}"
            reasons = reasons + ["incomplete_recording"]
            if not force and status != "skipped":
                status = "review"
            resolved.status, resolved.reasons = status, reasons

    # The recording row. Nothing is learned from it: no person confirmed these values.
    payload = _confirm_payload_from_resolved(resolved, scan)
    payload["unattended"] = not force
    if resolved.kind == "studio" and mb_release_id:
        payload["mb_release_id"] = mb_release_id
        payload["mb_release_status"] = "matched"
    if resolved.kind == "studio":
        payload["completeness_checked"] = True
    if "incomplete_release" in reasons or "incomplete_recording" in reasons:
        payload["is_complete"] = False
    if not keep_saved:
        apply_blanket_values(resolved, payload, applied)
    payload["resolver_result"] = resolved.to_dict()
    # A kind of "unknown" is stored as live; the row's reason (needs_type) keeps it open.
    if payload.get("kind") not in ("live", "studio"):
        payload["kind"] = "live"
    rel = (item.rel_path if item is not None else os.path.basename(folder_abs.rstrip("/\\")))
    if keep_saved:
        rec = own
    else:
        rec = save_recording_payload(payload, user_id, own, location="queue", folder_path=rel,
                                     source_folder=folder_abs)
    if item is not None and item.recording_id != rec.id:
        item.recording_id = rec.id          # linked before anything can fail below
    db.session.commit()

    # "Ingest anyway" still refuses a folder with no artist or no type.
    if status == "review" and not (force and "needs_artist" not in reasons
                                   and "needs_type" not in reasons):
        return {"status": "review", "reasons": reasons, "format": fmt, "detail": detail,
                "resolved": resolved, "result": None, "recording_id": rec.id, "duplicate_of": None}

    if hold:
        return {"status": "ready", "reasons": reasons, "format": fmt, "detail": None,
                "resolved": resolved, "result": None, "recording_id": rec.id, "duplicate_of": None}

    result = promote_to_library(rec, user_id, payload, source_folder=folder_abs,
                                progress_cb=progress_cb, cancel_cb=cancel_cb, phase_cb=phase_cb)
    return {"status": "ingested", "reasons": reasons, "format": fmt, "detail": None,
            "resolved": resolved, "result": result, "recording_id": rec.id, "duplicate_of": None}


def read_folder_into_recording(folder_abs, location, user_id):
    """Read a folder that has no recording and give it one at `location` ('workshop' or
    'backlog'): the Review form and a Move both start from a recording. Held, so nothing
    is imported. Returns (recording, outcome); recording is None when the folder fails
    to read, or is an exact duplicate (outcome carries the reasons and `duplicate_of`).
    A folder that already is a recording at that folder is returned as it is."""
    from app.utils.resolve import recording_at_folder
    existing = recording_at_folder(folder_abs)
    if existing is not None and existing.location != "library":
        return existing, {"status": "ready", "reasons": []}
    out = auto_confirm(folder_abs, user_id, hold=True)
    rec = db.session.get(Recording, out["recording_id"]) if out.get("recording_id") else None
    if rec is None:
        return None, out
    rec.location = location
    rec.folder_path = os.path.basename(folder_abs.rstrip("/\\"))
    db.session.commit()
    return rec, out


def _overlay_saved(resolved, rec):
    """The values saved on a recording replace the folder's reading, the way staged
    blanket values do, so the verdict is about what a person left there."""
    perf, venue = rec.performance, rec.performance.venue

    def put(name, value):
        setattr(resolved, name, ResolvedField(value=value, source="applied", candidates={}, conflict=False))
    put("artist", perf.artist.name if has_artist(rec) else None)
    put("date", {"year": perf.start_year, "month": perf.start_month, "day": perf.start_day})
    put("venue", venue.name if venue else None)
    put("city", venue.city if venue else perf.city)
    put("state", venue.state if venue else perf.state)
    put("country", venue.country if venue else perf.country)
    put("event", perf.event.name if perf.event else None)
    put("stage", perf.stage)
    put("source", rec.source)
    put("lineage", rec.lineage)
    put("source_tag", rec.source_tag)
    put("shnid", rec.etree_shnid)
    put("album", rec.title)
    if rec.location != "queue" or kind_chosen(rec):
        resolved.kind = rec.kind


KIND_EVENT = "kind_chosen"


def kind_chosen(rec):
    """True once a person has picked Album or Live Recording on this recording's form."""
    return db.session.query(RecordingEvent.id).filter(
        RecordingEvent.recording_id == rec.id, RecordingEvent.event_type == KIND_EVENT).first() is not None


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
    if data.get("recording_id"):
        # Import of a recording that already exists: the folder comes from the
        # recording, never from the client.
        from app.utils.paths import queue_item_for
        from app.utils import bulk_ingest_run as _bir
        rec = db.session.get(Recording, data["recording_id"])
        if rec is None or rec.location == "library":
            return jsonify({"error": "Recording not found, or already in the library"}), 404
        if rec.location == "queue":
            qi = queue_item_for(rec)
            source_folder = _bir.item_abs_path(qi) if qi is not None else ""
        else:
            source_folder = recording_folder_abs(rec) or ""
        data["source_folder_path"] = source_folder
    if not source_folder or not os.path.isdir(source_folder):
        return jsonify({"error": f"Source folder not found: {source_folder!r}"}), 400
    from app.utils.download_queue import downloading_here
    if downloading_here(source_folder):
        return jsonify({"error": "This folder is still downloading."}), 409
    if not artist_name:
        return jsonify({"error": "artist_name is required"}), 400
    # R3: the Add Recording shnid field is free text. etree_shnid used to
    # be coerced to int only when building Recording in step 7 of
    # _do_confirm, AFTER step 6 had already moved the source into the
    # library -- a stray non-digit character there failed the job
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
    """Everything currently moving through ingestion, for the debug drawer's
    Ingest pane.

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


# ── A recording's life before the library ──────────────────────────────────────
# A recording row exists from the moment its folder is first read into the Queue
# (location 'queue'), is edited in place by the Review form (save_recording_payload,
# autosaved), and becomes a library recording when imported (promote_to_library).
# _do_confirm is those two steps in a row, for a caller that has a payload and a
# folder.

from app.utils.orphans import UNKNOWN_ARTIST_NAME
DRAFT_EVENT = "draft_edited"


def person_edited(rec):
    """True once a person has saved this recording from the Review form."""
    return db.session.query(RecordingEvent.id).filter(
        RecordingEvent.recording_id == rec.id, RecordingEvent.event_type == DRAFT_EVENT).first() is not None


def mark_person_edited(rec, user_id):
    if not person_edited(rec):
        db.session.add(RecordingEvent(recording_id=rec.id, user_id=user_id, event_type=DRAFT_EVENT,
                                      note="Edited in the Review form"))


def has_artist(rec):
    perf = rec.performance
    return bool(perf is not None and perf.artist is not None
                and perf.artist.name and perf.artist.name != UNKNOWN_ARTIST_NAME)


def _unknown_artist():
    """The stand-in act of a queue row whose folder named none. Never confirmed, and
    removed again with the last row that uses it (app.utils.orphans)."""
    a = db.session.query(Artist).filter(func.lower(Artist.name) == UNKNOWN_ARTIST_NAME.lower()).first()
    if a is None:
        a = Artist(name=UNKNOWN_ARTIST_NAME)
        db.session.add(a)
        db.session.flush()
    return a


def _delete_track_rows(track_ids):
    from app.models.track_analysis import TrackAnalysis
    from app.models.play_log import PlayLog
    if not track_ids:
        return
    db.session.query(TrackAnalysis).filter(TrackAnalysis.track_id.in_(track_ids)).delete(synchronize_session=False)
    db.session.query(PlayLog).filter(PlayLog.track_id.in_(track_ids)).delete(synchronize_session=False)
    db.session.query(Track).filter(Track.id.in_(track_ids)).delete(synchronize_session=False)


def _sync_tracks(rec, tracks_in):
    """Make rec's tracks match the form's list. A track is matched by the file it
    names (its current path, else the path it had when ingested); new ones are added,
    ones no longer listed are removed. file_path is never changed here."""
    existing = list(rec.tracks)
    by_file = {t.file_path: t for t in existing}
    by_orig = {t.original_file_path: t for t in existing if t.original_file_path}
    seen = set()
    for t in tracks_in:
        fn = t.get("filename", "") or ""
        tr = by_file.get(fn) or by_orig.get(fn)
        if tr is not None and tr.id in seen:
            tr = None
        flags_raw = t.get("flags") or []
        vals = dict(
            track_number      = t.get("track_number"),
            title             = t.get("title") or f"Track {t.get('track_number', '?')}",
            set_number        = t.get("set_number") or None,
            disc_number       = t.get("disc_number"),
            disc_track_number = t.get("disc_track_number"),
            duration          = t.get("duration"),
            is_official       = bool(rec.is_official or t.get("is_official", False)),
            flags             = json.dumps(flags_raw) if flags_raw else None,
            songwriter        = t.get("songwriter") or None,
            notes             = t.get("notes") or None,
        )
        if tr is None:
            tr = Track(recording_id=rec.id, file_path=fn, original_file_path=fn or None, **vals)
            db.session.add(tr)
            db.session.flush()
        else:
            for k, v in vals.items():
                setattr(tr, k, v)
        seen.add(tr.id)
    _delete_track_rows([t.id for t in existing if t.id not in seen])
    db.session.flush()
    db.session.expire(rec, ["tracks"])


def _match_fingerprints(rec, *, verify, folder_abs=None):
    """Match rec's stored fingerprint files to its tracks (expected checksum and type).
    With verify, a track still lacking a status is checked now: FFP/ST5 against the
    FLAC header, MD5 left 'unverified' (never re-hashed here). Never overwrites a
    status a track already has."""
    tracks = list(rec.tracks)
    if not tracks:
        return
    now = datetime.now(timezone.utc)
    origs = {t: (t.original_file_path or t.file_path) for t in tracks}
    for fp in sorted(rec.fingerprints, key=lambda f: FINGERPRINT_TYPE_PRIORITY.get(f.fingerprint_type, 9)):
        if not fp.content:
            continue
        fp_dir = os.path.dirname(fp.rel_path or "").replace(os.sep, "/")
        cands = [t for t in tracks
                 if os.path.dirname(origs[t]).replace(os.sep, "/") == fp_dir] if fp_dir else tracks
        cands = cands or tracks
        proxies = [ChecksumMatchProxy(t, origs[t]) for t in cands]
        for proxy, expected in match_entries_to_tracks(parse_checksum_file(fp.content), proxies).items():
            t = proxy.real
            if t.checksum_status is not None and t.expected_checksum:
                continue
            t.checksum_type, t.expected_checksum = fp.fingerprint_type, expected
    if verify:
        root = folder_abs or recording_folder_abs(rec)
        for t in tracks:
            if t.expected_checksum and t.checksum_status is None:
                if t.checksum_type == "md5":
                    t.checksum_status = "unverified"
                else:
                    t.checksum_status = verify_track_checksum(os.path.join(root, t.file_path),
                                                              t.checksum_type, t.expected_checksum)
                t.checksum_verified_at = now


def _store_fingerprints(rec, fingerprints, source_folder):
    """Archive the folder's fingerprint files on a recording that has none yet."""
    if rec.fingerprints or not fingerprints:
        return
    from app.utils.checksums import read_checksum_text
    for fp in sorted(fingerprints, key=lambda f: FINGERPRINT_TYPE_PRIORITY.get(f.get("type"), 9)):
        rel_path = fp.get("rel_path") or os.path.basename(fp.get("filename") or "")
        content = None
        if source_folder:
            try:
                content = read_checksum_text(os.path.join(source_folder, rel_path))
            except OSError:
                content = None
        db.session.add(RecordingFingerprint(
            recording_id=rec.id, fingerprint_type=fp.get("type"), filename=fp.get("filename"),
            content=content if content is not None else fp.get("content"), rel_path=rel_path))
    db.session.flush()
    db.session.expire(rec, ["fingerprints"])


def _form_tracks(rec):
    out = []
    for t in rec.tracks:
        try:
            flags = json.loads(t.flags) if t.flags else []
        except ValueError:
            flags = []
        out.append({"track_number": t.track_number, "title": t.title, "set_number": t.set_number,
                    "disc_number": t.disc_number, "disc_track_number": t.disc_track_number,
                    "duration": t.duration, "filename": t.file_path, "flags": flags,
                    "is_official": bool(t.is_official), "songwriter": t.songwriter, "notes": t.notes})
    return out


def recording_draft(rec):
    """The recording as the Review form holds it (the keys of its payload)."""
    perf = rec.performance
    venue, event, artist = perf.venue, perf.event, perf.artist
    eng = lambda e: e.name if e is not None else ""
    from app.utils.personnel import resolve_performance_personnel
    lineup = resolve_performance_personnel(perf) if has_artist(rec) else []
    return {
        "artist_name": artist.name if has_artist(rec) else "",
        "start_year": perf.start_year or "", "start_month": perf.start_month or "",
        "start_day": perf.start_day or "",
        "end_year": perf.end_year or "", "end_month": perf.end_month or "", "end_day": perf.end_day or "",
        "venue_name": venue.name if venue else "", "venue_id": venue.id if venue else None,
        "city": (venue.city if venue else perf.city) or "",
        "state": (venue.state if venue else perf.state) or "",
        "country": (venue.country if venue else perf.country) or "",
        "event_name": event.name if event else "", "event_id": event.id if event else None,
        "stage": perf.stage or "",
        "source": rec.source or "", "quality": rec.quality or "", "lineage": rec.lineage or "",
        "source_tag": rec.source_tag or "",
        "etree_shnid": str(rec.etree_shnid) if rec.etree_shnid is not None else "",
        "recording_engineer": eng(rec.recording_engineer), "transfer_engineer": eng(rec.transfer_engineer),
        "mastering_engineer": eng(rec.mastering_engineer),
        "notes": rec.notes or "", "is_official": bool(rec.is_official), "title": rec.title or "",
        "kind": rec.kind, "info_file_content": rec.info_file_content,
        "genre_id": artist.genre_id if artist.genre_id and has_artist(rec) else None,
        "genre_name": artist.genre.name if artist.genre_id and has_artist(rec) else "",
        "members": [{"id": r["musician_id"], "name": r["name"]} for r in lineup if not r["is_guest"]],
        "guests":  [{"id": r["musician_id"], "name": r["name"]} for r in lineup if r["is_guest"]],
        "tracks": _form_tracks(rec),
    }




def save_recording_payload(data, user_id, rec=None, *, location="queue", folder_path=None,
                           source_folder=None):
    """Create or update a recording and the chain above it (Artist, Venue, Event,
    Performance, personnel, Engineers) and its Tracks from a Review-form payload.
    Flushes; the caller commits. A recording that exists is updated in place: only
    keys the payload carries are written, so a partial payload is fine (the Workshop
    is a workspace; nothing here requires a complete recording).

    Nothing here is a person's confirmation: Artist.confirmed_at, learned aliases and
    the Queue re-check belong to confirm_signals().

    Returns the Recording."""
    if rec is not None:
        # A key the payload leaves out keeps what the recording has (a partial autosave).
        cur_draft = recording_draft(rec)
        for k in ("artist_name", "start_year", "start_month", "start_day", "end_year", "end_month",
                  "end_day", "city", "state", "country", "stage"):
            data.setdefault(k, cur_draft[k])
        if "venue_id" not in data and "venue_name" not in data:
            data["venue_id"], data["venue_name"] = cur_draft["venue_id"], cur_draft["venue_name"]
        if "event_id" not in data and "event_name" not in data:
            data["event_id"], data["event_name"] = cur_draft["event_id"], cur_draft["event_name"]
        if "etree_shnid" not in data:
            data["etree_shnid"] = rec.etree_shnid
    _raw_shnid = data.get("etree_shnid")
    if _raw_shnid not in (None, ""):
        try:
            data["etree_shnid"] = int(str(_raw_shnid).strip())
        except (TypeError, ValueError):
            raise ValueError(f"shnid must be a number: {_raw_shnid!r}")
    else:
        data["etree_shnid"] = None

    from app.utils.orphans import collect_ids, cleanup as _cleanup_orphans
    before = collect_ids(rec)

    artist_name = (data.get("artist_name") or "").strip()
    city        = (data.get("city")       or "").strip() or None
    state       = (data.get("state")      or "").strip() or None
    country     = (data.get("country")    or "").strip() or None
    venue_name  = (data.get("venue_name") or "").strip() or None
    event_name  = (data.get("event_name") or "").strip() or None
    stage       = (data.get("stage") or "").strip() or None
    start_year, start_month, start_day = (data.get("start_year") or None, data.get("start_month") or None,
                                          data.get("start_day") or None)
    end_year, end_month, end_day = (data.get("end_year") or None, data.get("end_month") or None,
                                    data.get("end_day") or None)

    rec_kind = data.get("kind") or (rec.kind if rec is not None else "live")
    if rec_kind not in ("live", "studio"):
        raise ValueError(f"kind must be 'live' or 'studio': {rec_kind!r}")

    # 1. Artist. members/guests: None means "leave that bucket as it is" (see sync_performance_personnel).
    member_names_sync = data.get("members")
    guest_names_sync  = data.get("guests")
    if artist_name:
        artist = resolve_or_create_artist(artist_name, member_names_sync or [], lookup=False)
        _apply_artist_genre(artist, data)
    else:
        artist = _unknown_artist()

    # 2. Venue (placeholder names are never a venue).
    venue = None
    venue_id_in = data.get("venue_id")
    if venue_id_in:
        candidate = db.session.get(Venue, int(venue_id_in))
        if candidate and not is_placeholder_venue_name(candidate.name):
            venue = candidate
    elif venue_name and not is_placeholder_venue_name(venue_name):
        venue = db.session.query(Venue).filter(func.lower(Venue.name) == venue_name.lower()).first()
        if not venue:
            venue = Venue(name=venue_name, city=city, state=state, country=country)
            db.session.add(venue)
            db.session.flush()

    # 3. Event
    event = None
    event_id_in = data.get("event_id")
    if event_id_in:
        event = db.session.get(Event, int(event_id_in))
    elif event_name:
        event = _find_event(event_name)
        if not event and data.get("event_create", True) and clean_event_name(event_name):
            event = Event(name=clean_event_name(event_name), city=city, state=state, country=country)
            db.session.add(event)
            db.session.flush()

    # 4. Performance: the show this recording belongs to. A live show shared with
    # another recording is joined; a recording that is alone on its Performance
    # keeps it and edits it in place, so each autosave does not mint a new one.
    cur = db.session.get(Performance, rec.performance_id) if rec is not None and rec.performance_id else None
    sole = cur is not None and db.session.query(Recording.id).filter(
        Recording.performance_id == cur.id, Recording.id != rec.id).first() is None
    matching = []
    if rec_kind != "studio" and artist_name:         # an unknown act never joins another show
        q = db.session.query(Performance).filter(
            Performance.artist_id == artist.id, Performance.start_year == start_year,
            Performance.start_month == start_month, Performance.start_day == start_day)
        if venue:
            q = q.filter(Performance.venue_id == venue.id)
        matching = q.order_by(Performance.id).all()
    performance = None
    if cur is not None and cur in matching:
        performance = cur
    elif matching:
        performance = matching[0]
    elif cur is not None and sole:
        performance = cur
    fields = dict(artist_id=artist.id, venue_id=venue.id if venue else None,
                  event_id=event.id if event else None, stage=stage,
                  start_year=start_year, start_month=start_month, start_day=start_day,
                  end_year=end_year, end_month=end_month, end_day=end_day,
                  city=city if not venue else None, state=state if not venue else None,
                  country=country if not venue else None)
    if performance is None:
        performance = Performance(personnel_mode=artist.default_personnel_mode, **fields)
        db.session.add(performance)
    elif performance is cur and sole:
        for k, v in fields.items():
            setattr(performance, k, v)
    db.session.flush()
    sync_performance_personnel(performance, member_names_sync, guest_names_sync)

    # 5. Recording
    created = rec is None
    if created:
        rec = Recording(performance_id=performance.id, location=location,
                        folder_path=folder_path or "",
                        original_folder_name=os.path.basename((source_folder or "").rstrip("/\\")) or None,
                        kind=rec_kind)
        db.session.add(rec)
    rec.performance_id = performance.id
    rec.kind = rec_kind
    if folder_path:
        rec.folder_path = folder_path
    for key, attr, conv in (("source", "source", None), ("lineage", "lineage", None),
                            ("quality", "quality", None), ("notes", "notes", None),
                            ("info_file_content", "info_file_content", None),
                            ("title", "title", lambda v: v or None),
                            ("source_tag", "source_tag", lambda v: (v or None)),
                            ("etree_shnid", "etree_shnid", None),
                            ("is_complete", "is_complete", bool), ("is_official", "is_official", bool)):
        if key in data or (created and key == "is_complete"):
            v = data.get(key, True if key == "is_complete" else None)
            setattr(rec, attr, conv(v) if conv else v)
    if "resolver_result" in data:
        rec.resolver_json = resolver_json_for_storage(data.get("resolver_result"))
    if data.get("completeness_checked"):
        rec.completeness_checked_at = datetime.now(timezone.utc)
    db.session.flush()
    from app.utils.engineers import apply_engineer_names
    apply_engineer_names(rec, data)
    if created and source_folder:
        lomax.repoint_folder(source_folder, rec.id)

    # 6. Tracks and the folder's fingerprint files
    if "tracks" in data:
        _sync_tracks(rec, data.get("tracks") or [])
    _store_fingerprints(rec, data.get("fingerprints") or [], source_folder)
    _match_fingerprints(rec, verify=False)
    db.session.flush()

    # What this save left with nothing on it (a half-typed venue, the old Performance,
    # the stand-in act) goes, unless somebody confirmed or enriched it.
    _cleanup_orphans(before)
    db.session.flush()
    return rec


def confirm_signals_pre(rec, data):
    """A person's save settles the act and teaches the aliases it quoted. Before the commit."""
    if data.get("unattended"):
        return
    perf = rec.performance
    if perf is None:
        return
    if has_artist(rec):
        mark_artist_confirmed(perf.artist)
    from app.utils.aliases import learn_aliases
    learn_aliases(data, perf.artist if has_artist(rec) else None, perf.venue)


def confirm_signals_post(rec, data, source_folder):
    """After the commit: the other waiting Queue rows that quote the same act, venue or
    event are looked at again."""
    if data.get("unattended"):
        return
    try:
        from app.utils.aliases import confirmed_keys
        from app.utils import bulk_ingest_run as _bir
        perf = rec.performance
        ev = perf.event.name if perf is not None and perf.event is not None else data.get("event_name")
        _bir.recheck_queue(source_folder, confirmed_keys(
            data, perf.artist if perf is not None and has_artist(rec) else None,
            perf.venue if perf is not None else None, ev))
    except Exception:  # noqa: BLE001 -- never fails a save that already succeeded
        db.session.rollback()


def recording_for_folder(folder_abs, data=None):
    """The recording a folder already is: the id the payload names, else the Queue row
    linked to this folder, else the Workshop/Backlog recording sitting in it. None
    when the folder has no recording."""
    rid = (data or {}).get("recording_id")
    if rid:
        r = db.session.get(Recording, int(rid))
        if r is not None:
            return r
    from app.utils import bulk_ingest_run as _bir
    r = _bir.recording_of_folder(folder_abs)
    if r is not None:
        return r
    from app.utils.resolve import recording_at_folder
    return recording_at_folder(folder_abs)


def promote_to_library(rec, user_id, data=None, *, source_folder=None, progress_cb=None,
                       cancel_cb=None, phase_cb=None):
    """Import: move the recording's folder into the library under the File Handling
    mode (flatten and rename as set; nothing is moved when the folder already sits in
    the library outside a working folder), then make it a library recording: location
    'library', track paths, checksums, the 'ingested' event, artwork, the tag write.
    Commits. Returns the result dict _do_confirm has always returned.

    The files move BEFORE the database changes are committed; a failed move leaves
    the recording where it was."""
    from app.utils.ingest import IngestCancelled
    from app.utils.published import in_working_folder
    data = data or {}

    def _phase(key, detail=None):
        if phase_cb:
            phase_cb(key, detail)

    source_folder = (source_folder or recording_folder_abs(rec) or "").strip()
    if not source_folder or not os.path.isdir(source_folder):
        raise ValueError(f"Source folder not found: {source_folder!r}")
    if rec.location == "library":
        raise ValueError("This recording is already in the library")

    perf = rec.performance
    artist_name = perf.artist.name if has_artist(rec) else ""
    venue = perf.venue
    venue_name = venue.name if venue else None
    city = (venue.city if venue else perf.city)
    state = (venue.state if venue else perf.state)
    country = (venue.country if venue else perf.country)
    rec_kind = rec.kind

    file_handling = node_settings.get_file_handling()
    if file_handling["rename_folders"]:
        folder_name = build_folder_name(
            artist_name=artist_name, start_year=perf.start_year, start_month=perf.start_month,
            start_day=perf.start_day, venue_name=venue_name, city=city, state=state, country=country,
            source=rec.source, kind=rec_kind, title=rec.title)
    else:
        folder_name = os.path.basename(source_folder.rstrip("/\\")) or "Untitled"

    library_root = str(current_app.config["LIBRARY_ROOT"])
    under_artist = node_settings.file_under_artist_folder()
    try:
        in_library = Path(source_folder).resolve().is_relative_to(Path(library_root).resolve())
    except OSError:
        in_library = False
    # Catalogued in place: inside the library and not in a working folder that lives there.
    in_root_source = in_library and not in_working_folder(source_folder)

    tracks_in = [{"track_number": t.track_number, "title": t.title, "filename": t.file_path,
                  "disc_number": t.disc_number, "disc_track_number": t.disc_track_number,
                  "set_number": t.set_number} for t in rec.tracks]
    naming_scheme   = file_handling["naming_scheme"]
    naming_template = file_handling["naming_template"] or None
    if in_root_source:
        audio_rename_map = compute_audio_rename_map(tracks_in, "original")
        flatten = False
    elif file_handling["rename_files"]:
        audio_rename_map = compute_audio_rename_map(
            tracks_in, naming_scheme, naming_template, performance=perf,
            source=rec.source, source_tag=rec.source_tag, etree_shnid=rec.etree_shnid)
        flatten = flattens(naming_scheme, naming_template)
    else:
        audio_rename_map = compute_audio_rename_map(tracks_in, "original")
        flatten = False

    _phase("moving", folder_name)
    try:
        new_folder_path = move_to_library(
            source_folder=source_folder, library_root=library_root, artist_name=artist_name,
            folder_name=folder_name, progress_cb=progress_cb, audio_rename_map=audio_rename_map,
            cancel_cb=cancel_cb, under_artist_folder=under_artist, flatten=flatten,
            force_move=in_library and not in_root_source)
    except IngestCancelled:
        db.session.rollback()
        raise
    except Exception as e:
        db.session.rollback()
        if isinstance(e, OSError):
            raise MoveFailed(f"File operation failed: {e}")
        raise RuntimeError(f"File operation failed: {e}")
    folder_name = os.path.basename(new_folder_path)

    _phase("cataloging", f"{len(tracks_in)} track{'' if len(tracks_in) == 1 else 's'}")
    was = rec.location
    rec.location, rec.folder_path = "library", new_folder_path
    for tr in rec.tracks:
        tr.file_path = resolve_ingest_file_path(tr.file_path, audio_rename_map, flatten)
    db.session.flush()

    # Fingerprints: verify at the new location, the copy the app streams from.
    if rec.fingerprints:
        _phase("checksums", ", ".join(sorted({(f.fingerprint_type or "?").upper() for f in rec.fingerprints})))
        from app.utils.checksums import read_checksum_text
        for fp in rec.fingerprints:
            try:
                fp.content = read_checksum_text(os.path.join(library_root, new_folder_path,
                                                             fp.rel_path or "")) or fp.content
            except OSError:
                pass
    _match_fingerprints(rec, verify=True, folder_abs=os.path.join(library_root, new_folder_path))

    db.session.add(RecordingEvent(
        recording_id=rec.id, user_id=user_id, event_type="ingested",
        note=f"behavior=move original={os.path.basename(source_folder)}"
             + (f" from={was}" if was != "queue" else "")))

    # Lomax runs filed against this folder while it was being reviewed belong to the recording.
    lomax.repoint_folder(source_folder, rec.id)
    ai_result = data.get("ai_result")
    if ai_result and not db.session.query(LomaxRun).filter_by(
            subject_type="recording", subject_id=rec.id).first():
        lomax.adopt_result(rec.id, ai_result, user_id)

    _phase("saving")
    from app.utils.quality_store import promote_to_recording
    promote_to_recording(source_folder, rec.id, commit=False)
    confirm_signals_pre(rec, data)
    db.session.commit()

    mb_release_id = (data.get("mb_release_id") or "").strip() if isinstance(data.get("mb_release_id"), str) else ""
    if rec_kind == "studio" and mb_release_id:
        try:
            from app.utils import musicbrainz as _mbr
            _mbr.reset_breaker()
            details = _mbr.lookup_release(mb_release_id)
            if details:
                how = "matched" if data.get("mb_release_status") == "matched" else "linked"
                _mbr.apply_to_recording(rec, details, status=how)
                _mbr.enrich_from_release(rec)
                db.session.commit()
        except Exception:  # noqa: BLE001 -- the recording is saved either way
            db.session.rollback()
            import traceback as _tb6
            _tb6.print_exc()

    created_tracks = list(rec.tracks)
    checksum_mismatches = sum(1 for t in created_tracks if t.checksum_status == "mismatch")

    tag_errors = []
    if file_handling["write_tags_on_ingest"] and not in_root_source:
        _phase("tags", f"{len(created_tracks)} tracks")
        n_written, tag_errors = write_flac_tags(rec, library_root)
        if n_written > 0:
            note = f"{n_written} file(s) written"
            if tag_errors:
                note += f"; {len(tag_errors)} error(s): " + "; ".join(f[0] for f in tag_errors)
            db.session.add(RecordingEvent(recording_id=rec.id, user_id=user_id,
                                          event_type="tags_written", note=note))
            db.session.commit()

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

    confirm_signals_post(rec, data, source_folder)
    try:
        from app.utils import bulk_ingest_run as _bir
        _bir.mark_folder_ingested(source_folder, rec)
    except Exception:  # noqa: BLE001
        db.session.rollback()
        import traceback as _tb5
        _tb5.print_exc()

    return {
        "recording_id":        rec.id,
        "artist_id":           perf.artist_id,
        "folder_name":         folder_name,
        "event_id":            perf.event_id,
        "stage":               perf.stage,
        "checksum_mismatches": checksum_mismatches,
        "tag_errors":          tag_errors,
        "image_errors":        image_errors,
    }


def _do_confirm(data, user_id, progress_cb=None, cancel_cb=None, phase_cb=None):
    """Import a folder from a payload: save the payload onto the folder's recording
    (creating the recording when the folder has none, so a folder never gets a
    second one) and promote it to the library. Runs inside an app context
    (background thread). Returns a result dict; raises on failure.

    Payload keys (all optional except source_folder_path and artist_name):
      recording_id, source_folder_path, artist_name, start_year/month/day,
      end_year/month/day, venue_name, venue_id, city, state, country, event_name,
      event_id, event_create, stage, source, source_tag, etree_shnid, quality,
      lineage, notes, is_complete, is_official, title, kind, info_file_content,
      resolver_result, ai_result, mb_release_id, members, guests, genre_id,
      genre_name, recording_engineer, transfer_engineer, mastering_engineer,
      unattended (no person confirmed it: nothing is learned from the save),
      fingerprints [{type, filename, content, rel_path}],
      tracks [{track_number, title, set_number, disc_number, disc_track_number,
               duration, filename, flags, is_official, songwriter, notes}].

    MusicBrainz and the non-music signal never run inline: the follow-ups do
    (enqueue_followups). MD5 is never re-hashed here; FFP/ST5 headers are checked.
    """
    def _phase(key, detail=None):
        if phase_cb:
            phase_cb(key, detail)

    source_folder = (data.get("source_folder_path") or "").strip()
    _phase("resolving", (data.get("artist_name") or "").strip() or None)
    rec = recording_for_folder(source_folder, data)
    created = rec is None
    if rec is not None and rec.location == "library":
        raise ValueError("This recording is already in the library")
    try:
        rec = save_recording_payload(
            data, user_id, rec, location="queue",
            folder_path=os.path.basename(source_folder.rstrip("/\\")) if created else None,
            source_folder=source_folder)
        return promote_to_library(rec, user_id, data, source_folder=source_folder,
                                  progress_cb=progress_cb, cancel_cb=cancel_cb, phase_cb=phase_cb)
    except Exception:
        db.session.rollback()          # a recording created by this call leaves no trace
        raise



import re as _re
from collections import Counter as _Counter

_TEXT_EXTS   = {'.txt', '.nfo', '.md', '.text', '.log'}
_LOSSY_EXTS  = {'.mp3', '.aac', '.ogg', '.m4a'}
_LOSSLESS_EXTS = {'.flac', '.wav', '.aiff', '.aif', '.ape', '.wv'}
# Canonical home is app/utils/ingest.py (FOLDER_DATE_RE). Aliased here so _audit_incoming_folder and
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
    """POST /api/ingest/batch-scan
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
              under "yellow"). Now it's just an alias so the pill counts and
              the "Auto-Ingest All ___" filters always match what's on screen.
      - confidence: { artist, date, tracks, venue }  — per-field scores, still
                    used for the "uncertain" styling on individual fields in
                    the expanded row detail (unrelated to tier now).
      - extracted: { artist, year, month, day, venue, city, state, country,
                     source, lineage, track_count, tracks_titled, format, kind }
                     format/kind computed the same way bulk_ingest.py's
                     extract()/classify() do, so this table's
                     FORMAT/TYPE pills match the other two ingest surfaces.
      - paula: compute_paula_score() result, or None (empty/unreadable folder).
               Purple-border source-of-truth in Add Recording's field-level
               confidence highlighting — no longer rendered as its own
               narrative/avatar anywhere in the UI.
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
    # scan uses, so Paula's per-item confidence scoring here matches exactly.
    known_artists = [p.name for p in db.session.query(Artist.name).all()]
    known_venues = [
        {"name": v.name, "city": v.city, "state": v.state, "country": v.country}
        for v in db.session.query(Venue).all()
    ]

    # Already-ingested folder paths (relative or basename match)
    ingested_paths = {
        os.path.basename(r.folder_path)
        for r in db.session.query(Recording.folder_path).filter(Recording.location == "library").all()
    }

    results = []

    # Resolve every top-level entry to its actual show paths before scanning.
    # Handles arbitrary nesting depth (artist → year → show, etc.).
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
        # scan endpoint (app/api/recordings.py). Frontend aggregates these into
        # a single batch-level narrative rather than showing per-row scores.
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


# _audit_incoming_folder is used by batch-scan's filesystem audit.
