"""
api/quality.py — Listening Quality: analysis jobs and triage.

The first stage of the unified ingestion flow.  A folder (one show or a parent
holding many) is resolved to its shows, each is analysed, and the user accepts
or rejects each one before any metadata work happens.  LQ goes first because it
is the cheap objective gate: metadata review is the expensive human step, and
there is no point spending it on a recording that is not worth keeping.

Routes:
    POST /api/quality/analyze          start a background analysis job
    GET  /api/quality/analyze/<job_id> poll progress + results so far
    POST /api/quality/triage           accept / reject / reset one folder
    POST /api/quality/triage-bulk      accept or reject many at once
    GET  /api/quality/staging          rows for one scanned directory
    GET  /api/quality/recording/<id>   permanent score for one recording

Deliberately reused rather than rebuilt:
  * `utils.ingest.resolve_shows_in_dir()` — the same show resolution batch
    scanning uses, so the two lists can never disagree.
  * `/api/stream/ingest-preview` — pre-ingest audio playback already exists and
    is already IMPORT_ROOTS-guarded.  The standalone harness's own streaming
    endpoint deliberately does NOT come across.

Job state is in-memory, following the `/api/ingest/confirm` pattern.  It does
not survive a restart — acceptable because the STAGING ROWS are the durable
part: a restart mid-run loses the progress bar, not the analysis.
"""

import os
import threading
import traceback as _tb
import unicodedata
import uuid

from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required
from sqlalchemy import func

from app.extensions import db
from app.models.performer import Performer
from app.utils import quality_store as qs
from app.utils.ingest import resolve_shows_in_dir

bp = Blueprint("quality", __name__)

# job_id → {status, total, done, current, folders, error}
_QUALITY_JOBS = {}


# ═════════════════════════════════════════════════════════════════════════════
# Analysis job
# ═════════════════════════════════════════════════════════════════════════════
def _analyse_one(folder_path, source_dir):
    """
    Analyse a single show folder and upsert its staging row.

    Never raises: a folder that fails to decode records its error on the row so
    the UI can show WHY a card is empty.  One bad folder must not abort a
    50-folder run.
    """
    # Imported lazily — numpy/scipy/soundfile are heavyweight and only the
    # analysis path needs them, so app boot stays fast.
    from app.utils.quality import (extract_recording_features, score_recording,
                                   guess_source_from_name)

    name = os.path.basename(folder_path.rstrip("/"))
    try:
        features = extract_recording_features(folder_path)
        if "error" in features:
            qs.upsert_staging(folder_path, source_dir=source_dir, name=name,
                              error=str(features["error"]))
            return
        # Source is read off the folder name because this runs at TRIAGE time —
        # there is no Recording row yet. It matters: source is the strongest
        # single predictor of grade in the whole model (CV r = +0.314 on its
        # own), so skipping it here would throw away the largest accuracy gain
        # of the 2026-07-31 rework. Unreadable source is neutral, not a penalty.
        scored = score_recording(features,
                                 source=guess_source_from_name(name))
        qs.upsert_staging(folder_path, source_dir=source_dir, name=name,
                          scored=scored, features=features, error=None)
    except Exception as e:  # noqa: BLE001
        _tb.print_exc()
        try:
            qs.upsert_staging(folder_path, source_dir=source_dir, name=name,
                              error=str(e))
        except Exception:  # noqa: BLE001
            _tb.print_exc()


def _run_quality_job(job_id, app, source_dir, folders, reanalyze):
    """Background worker: analyse each folder in turn, updating job progress."""
    job = _QUALITY_JOBS[job_id]
    try:
        with app.app_context():
            for i, folder in enumerate(folders):
                job["current"] = os.path.basename(folder.rstrip("/"))
                job["done"] = i
                # Skip folders already analysed at the current engine version
                # unless explicitly asked to redo them — re-decoding audio is
                # the only genuinely slow thing here.
                if not reanalyze and _is_current(folder):
                    row = _adopt_into_scan(folder, source_dir)
                    # `list_staging` deliberately excludes rows already promoted
                    # to a Recording, so the client gets no row for these and
                    # cannot tell "already in your library" from "something
                    # broke". Name them explicitly rather than let it guess.
                    if row is not None and row.recording_id is not None:
                        job["ingested"].append(qs.norm_path(folder))
                    continue
                _analyse_one(folder, source_dir)
            job["done"] = len(folders)
            job["current"] = None
            job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        _tb.print_exc()
        job["error"] = str(e)
        job["status"] = "error"


def _adopt_into_scan(folder_path, source_dir):
    """
    Repoint an already-analysed row at the directory being scanned NOW.

    Staging rows are keyed by FOLDER PATH, but the triage list is fetched by
    SOURCE DIR (`qs.list_staging`).  The same show is reachable from several
    scanned directories — the Download root, the act folder inside it, the show
    folder itself — so a row first written under one of them is invisible to a
    scan of another.

    Until 2026-08-25 the skip path above returned without touching the row, so
    re-scanning at a different level returned ZERO rows from a job that
    reported "done": the UI's placeholder cards were never replaced and every
    recording sat on "Analysing…" forever, with no error anywhere to explain it
    (Ryan, 2026-08-25 — "Review and Ingest just stalls out").  Re-analysis
    already repoints `source_dir` (see `qs.upsert_staging`); the skip path
    simply never did, and that asymmetry WAS the bug.

    Writes only when the value actually changes.  The common case is re-scanning
    the same directory, and that must not cost a commit per folder.
    """
    row = qs.get_staging(folder_path)
    if row is None:                       # raced with a delete — nothing to do
        return None
    wanted = qs.norm_path(source_dir)
    if row.source_dir != wanted:
        row.source_dir = wanted
        db.session.commit()
    return row


def _is_current(folder_path):
    """
    True when this folder already has an analysis at the CURRENT engine version.

    Only `analysis_version` gates re-analysis.  A `score_version` bump is
    handled by re-scoring stored features with no audio decode — that split is
    the entire reason extraction and scoring are separate modules.
    """
    from app.utils.quality import QUALITY_ANALYSIS_VERSION

    row = qs.get_staging(folder_path)
    return (row is not None
            and row.error is None
            and row.analysis_version == QUALITY_ANALYSIS_VERSION)


@bp.route("/analyze", methods=["POST"])
@login_required
def analyze():
    """
    POST /api/quality/analyze
      { "source_dir": "/path/to/folder", "reanalyze": false }

    Resolves the directory to its show folders and starts a background job.
    Returns immediately with a job_id plus the resolved folder list, so the UI
    can render one placeholder card per show before any analysis finishes.
    """
    data = request.get_json() or {}
    source_dir = (data.get("source_dir") or "").strip()
    reanalyze = bool(data.get("reanalyze"))

    if not source_dir or not os.path.isdir(source_dir):
        return jsonify({"error": f"Directory not found: {source_dir!r}"}), 400

    # A single show folder passed directly is a legitimate case (it resolves to
    # itself), which is exactly why single-folder and bulk stopped being two
    # different features.
    from app.utils.ingest import resolve_shows
    folders = (resolve_shows(source_dir)
               if _has_root_audio(source_dir)
               else resolve_shows_in_dir(source_dir))

    if not folders:
        return jsonify({"error": "No audio folders found under that directory."}), 400

    job_id = uuid.uuid4().hex
    _QUALITY_JOBS[job_id] = {
        "status": "running", "total": len(folders), "done": 0,
        "current": None, "error": None, "ingested": [],
    }
    threading.Thread(
        target=_run_quality_job,
        args=(job_id, current_app._get_current_object(),
              source_dir, folders, reanalyze),
        daemon=True,
    ).start()

    return jsonify({
        "job_id": job_id,
        "source_dir": qs.norm_path(source_dir),
        "folders": [{"folder_path": qs.norm_path(f),
                     "name": os.path.basename(f.rstrip("/"))} for f in folders],
    }), 202


def _has_root_audio(path):
    from app.utils.ingest import _root_audio_count
    return _root_audio_count(path) > 0


@bp.route("/analyze/<job_id>", methods=["GET"])
@login_required
def analyze_status(job_id):
    """
    Poll an analysis job.

    Returns the CURRENT staging rows every time, not just at the end, so cards
    fill in as each ~2 s analysis lands rather than the user watching a bar for
    a minute.  The job entry is kept until the client has seen "done" once.
    """
    job = _QUALITY_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404

    source_dir = request.args.get("source_dir")
    rows = qs.list_staging(source_dir) if source_dir else []

    payload = {
        "status": job["status"],
        "total": job["total"],
        "done": job["done"],
        "current": job.get("current"),
        # Folders skipped because they are already Recordings. Not an error and
        # not in `results` — the client uses it to retire their placeholder
        # cards with a true explanation instead of spinning on "Analysing…".
        "ingested": list(job.get("ingested") or []),
        "results": [qs.serialize(r, include_features=True) for r in rows],
    }
    _attach_interpretation(payload["results"])
    _attach_metadata(payload["results"])
    _attach_fingerprints(payload["results"])
    _attach_concerns(payload["results"])
    if job["status"] == "error":
        payload["error"] = job["error"]
    if job["status"] in ("done", "error"):
        _QUALITY_JOBS.pop(job_id, None)
    return jsonify(payload)


# ═════════════════════════════════════════════════════════════════════════════
# Metadata completeness — the OTHER score on each triage card
# ═════════════════════════════════════════════════════════════════════════════
# folder_path → (folder_mtime, payload). A scan opens EVERY FLAC in the folder
# with mutagen to read its tags (~0.2 s for a 20-track show), and the triage
# page polls every ~1 s while analysing — so without this a 30-show bulk run
# would re-read several hundred FLAC headers per second for no reason.
#
# Keyed on the folder's own mtime so an edit to the info file, or files being
# added/removed, invalidates it naturally. Bounded because a single triage run
# only ever touches the folders under one scanned directory.
_META_CACHE = {}
_META_CACHE_MAX = 500


def _proposed_track_titles(scan, tags, info):
    """
    One entry per audio file: the title the ingest would propose, and whether
    it counts as REAL.

    Deliberately mirrors `_real_title_count()` in app/utils/health.py — tags
    preferred, info file as fallback, same `_is_real_title` predicate — so the
    list and the completeness score can never disagree about which titles are
    placeholders. Importing the predicate rather than reimplementing it is the
    point; two notions of "is this a real title" is how the panel would start
    lying about its own number.
    """
    from app.utils.health import _is_real_title

    tag_tracks  = tags.get("tracks") or []
    info_tracks = info.get("tracks") or []
    n = len(scan.get("audio_files") or [])

    out = []
    for i in range(n):
        title = tag_tracks[i].get("title") if i < len(tag_tracks) else ""
        if not _is_real_title(title) and i < len(info_tracks):
            title = info_tracks[i].get("title") or title
        out.append({"n": i + 1, "title": title or "",
                    "real": bool(_is_real_title(title))})
    return out


def _tag_date_parts(concert_date):
    """
    Split a FLAC CONCERTDATE-style tag ("YYYY-MM-DD", "YYYY-MM", or "YYYY")
    into (year, month, day) ints, any of which may be None.

    Mirrors the ingest wizard's own tags.concert_date.split('-') in app.js
    exactly (2026-08-09) — Triage and Add Recording must read the same tag
    the same way, or they can silently disagree about a recording's date.
    """
    if not concert_date:
        return None, None, None
    parts = str(concert_date).split('-')

    def _int(i):
        try:
            return int(parts[i]) if i < len(parts) and parts[i] else None
        except (ValueError, TypeError):
            return None

    return _int(0), _int(1), _int(2)


def _scan_metadata(folder_path):
    """
    Metadata suggestions + completeness score for one folder, cached.

    Uses `build_scan_payload()` — NOT raw `scan_folder()` — because that is the
    shared foundation both Add Recording and batch import score from, and
    `compute_health()` reads `suggestions.from_tags` / `from_info_file` which
    only exist on the full payload. Feeding it a bare `scan_folder()` result
    silently produces a score of 0 for every folder.
    """
    from app.utils.ingest import build_scan_payload

    try:
        mtime = os.path.getmtime(folder_path)
    except OSError:
        mtime = None

    hit = _META_CACHE.get(folder_path)
    if hit and hit[0] == mtime:
        return hit[1]

    scan = build_scan_payload(folder_path)
    if scan is None:                      # no audio in the folder
        payload = {"health": None, "extracted": None}
    else:
        sug = (scan.get("suggestions") or {}).get("from_info_file") or {}
        tags = (scan.get("suggestions") or {}).get("from_tags") or {}
        # Bug (Ryan, 2026-08-09): from_tags has no "year"/"month"/"day" keys —
        # the FLAC date tag lands in "concert_date" ("YYYY-MM-DD"), same as the
        # ingest wizard reads (see app.js's own tags.concert_date.split('-')).
        # tags.get("year") was therefore always None, so triage silently fell
        # through to from_info_file's parsed date on EVERY recording, with no
        # tag-date fallback at all — the ingest wizard prefers the tag date and
        # only falls back to the info file, so the two could disagree whenever
        # the info file's own date line parsed differently (Art Blakey and the
        # Jazz Messengers 1981-07-11 showing as 2026-07-11 in Triage but
        # correctly in Review). _tag_date_parts() mirrors the wizard's split so
        # both read the same date the same way.
        tag_year, tag_month, tag_day = _tag_date_parts(tags.get("concert_date"))
        payload = {
            # build_scan_payload already computed this; recomputing would just
            # risk the two disagreeing.
            "health": scan.get("health"),
            "extracted": {
                "artist":  tags.get("artist") or sug.get("artist"),
                "year":    tag_year  or sug.get("year"),
                "month":   tag_month or sug.get("month"),
                "day":     tag_day   or sug.get("day"),
                "venue":   tags.get("venue")  or sug.get("venue"),
                "city":    tags.get("city")   or sug.get("city"),
                "state":   tags.get("state")  or sug.get("state"),
                "country": tags.get("country") or sug.get("country"),
                "source":  tags.get("source") or sug.get("source"),
                "lineage": tags.get("lineage") or sug.get("lineage"),
                "track_count": len(scan.get("audio_files") or []),
                # The titles themselves, not just how many are populated
                # (2026-08-02). A ratio says the fields are filled; it cannot
                # show that they are filled with "Jam >" and "Unknown".
                "track_titles": _proposed_track_titles(scan, tags, sug),
            },
        }

    if len(_META_CACHE) >= _META_CACHE_MAX:
        _META_CACHE.clear()
    _META_CACHE[folder_path] = (mtime, payload)
    return payload


def _attach_interpretation(results):
    """
    Add the plain-English reading of each score to every row.

    Cheap enough to do on every poll: `interpret_full` is a pure function over
    the already-stored feature dict, so this costs a JSON parse and some
    lookups — no audio, no filesystem.

    The FULL interpretation goes in, metrics and range ladders included, because
    the card renders its whole detail panel up front and just hides it — same as
    the standalone harness. That costs payload, so the client stops polling once
    analysis finishes rather than re-fetching it every second forever.
    """
    from app.utils.quality import interpret_full

    for r in results:
        r["interp"] = None
        if r.get("error") or r.get("listening_quality") is None:
            continue
        try:
            # Rows arrive serialized WITH features (include_features=True); the
            # raw dict is popped again below — the interpretation supersedes it.
            r["interp"] = interpret_full(r, r.get("features") or {})
        except Exception:  # noqa: BLE001
            _tb.print_exc()
        finally:
            r.pop("features", None)


def _attach_metadata(results):
    """
    Add the metadata completeness score and extracted fields to each row.

    The triage card shows BOTH numbers because they answer different questions:
    Listening Quality is "is this worth keeping?", completeness is "how much
    typing will it cost me?".  Deciding to straight-ingest versus hand-edit
    needs both, and making the user visit two screens to see them would defeat
    the point of merging the flows.

    Scan failures are swallowed per-row: a folder whose metadata cannot be read
    still has a perfectly good audio score and must not vanish from triage.
    """
    for r in results:
        r["health"] = None
        r["extracted"] = None
        # Staging rows are keyed by folder path and outlive the folder itself:
        # a MOVE ingest relocates the source into the library, so the row is
        # still here but the directory is not. Without this flag the UI offered
        # to ingest a folder that no longer existed, showed a blank metadata
        # score, and failed with "artist_name is required" when clicked
        # (2026-07-31).
        r["exists"] = bool(r.get("folder_path")) and os.path.isdir(r["folder_path"])
        if r.get("error") or not r["exists"]:
            continue
        try:
            r.update(_scan_metadata(r["folder_path"]))
        except Exception:  # noqa: BLE001
            _tb.print_exc()


# folder_path → (mtime, deep, audit). Same mtime keying as _META_CACHE. `deep`
# is part of the key so a deep pass replaces a cheap one rather than being
# served the stale cheap answer.
_FP_CACHE = {}
_FP_CACHE_MAX = 400


def _fingerprint_audit(folder_path, deep=False):
    """Cheap-by-default fingerprint audit for one folder, cached."""
    from app.utils.checksums import audit_folder_fingerprints
    from app.utils.ingest import scan_folder

    try:
        mtime = os.path.getmtime(folder_path)
    except OSError:
        mtime = None
    hit = _FP_CACHE.get(folder_path)
    # A cached DEEP result satisfies a cheap request; the reverse is not true.
    if hit and hit[0] == mtime and (hit[1] or not deep):
        return hit[2]

    scan  = scan_folder(folder_path)
    audit = audit_folder_fingerprints(folder_path, (scan or {}).get("audio_files") or [],
                                      deep=deep)
    if len(_FP_CACHE) >= _FP_CACHE_MAX:
        _FP_CACHE.clear()
    _FP_CACHE[folder_path] = (mtime, deep, audit)
    return audit


def _attach_fingerprints(results):
    """
    Add the fingerprint audit to each row — the third triage tab.

    Cheap pass only (FFP/ST5, header reads). MD5 files are parsed and matched
    but their tracks report "pending" until an explicit deep verify, because
    hashing whole files across a queue is minutes, not seconds. See
    CHEAP_FP_TYPES in app/utils/checksums.py.
    """
    for r in results:
        r["fingerprints"] = None
        if r.get("error") or not r.get("exists"):
            continue
        try:
            r["fingerprints"] = _fingerprint_audit(r["folder_path"], deep=False)
        except Exception:  # noqa: BLE001
            _tb.print_exc()


def _attach_concerns(results):
    """
    The MAJOR-ISSUE line under each card title (Ryan, 2026-08-02).

    That space used to hold a prose description of how the recording sounds
    ("A listenable recording with obvious character…"), which restated the
    Sound Quality band in more words. It now carries only things that should
    stop you before you ingest — and stays empty when there are none, so its
    presence is itself the signal.

    Must run AFTER _attach_interpretation / _attach_metadata / _attach_
    fingerprints: it reads all three rather than recomputing anything.
    """
    from app.api.ingest import resolve_similar_performer_ids
    from app.models.performance import Performance
    from app.utils.format import format_partial_date

    for r in results:
        concerns = []
        x = r.get("extracted") or {}
        h = r.get("health") or {}
        fp = r.get("fingerprints") or {}

        # A promoted row (already ingested — see qs.promote_to_recording) is
        # EXPECTED to have a dead folder_path once ingest used Move: that's
        # what a successful move-and-triage-a-folder-again looks like, not a
        # problem. Staging rows outlive the folder on purpose (list_staging
        # matches on the scanned source_dir, so re-triaging the same parent
        # folder later resurfaces old, already-ingested rows right alongside
        # new ones) — this used to flag that expected state as a red error
        # (Ryan, 2026-08-09: "Source folder no longer exists on disk" showing
        # on a recording already marked Ingested, reproduced on Danny Gatton
        # and Charles Mingus folders). Only warn when the folder is ACTUALLY
        # missing for a row nothing has ingested yet — that's the case where
        # someone renamed/deleted a folder out from under a pending triage.
        if r.get("exists") is False and not r.get("recording_id"):
            concerns.append({"level": "error", "kind": "missing",
                             "text": "Source folder no longer exists on disk"})

        # Nothing to ingest at all. Worth saying loudly — the card would
        # otherwise look merely low-scoring rather than empty.
        if r.get("exists") and not x.get("track_count"):
            concerns.append({"level": "error", "kind": "no_audio",
                             "text": "No audio files detected in this folder"})

        # A failed checksum is the strongest signal available that a file is
        # damaged. It outranks every soft quality reading on the card.
        if fp.get("summary", {}).get("mismatch"):
            n = fp["summary"]["mismatch"]
            concerns.append({"level": "error", "kind": "checksum",
                             "text": f"{n} track{'' if n == 1 else 's'} failed checksum verification"})

        # A checksum FILE that couldn't be read at all (corrupt, unreadable,
        # gone missing between scan and read) is a different failure than a
        # mismatch — utils/checksums.py catches the OSError, tags that file
        # with "error", and skips it out of `summary` entirely, so nothing
        # above ever counted it. Left alone, that file's tracks silently fall
        # back to "unverified" — the same badge as "we just haven't run the
        # deep check yet" — and the actual read failure is visible only if
        # you open the Fingerprints tab and read that one file's row (Ryan,
        # 2026-08-27: "a failed fingerprint scan" must be exposed at
        # ingestion prep, not buried a click away).
        errored_fp_files = [f["filename"] for f in (fp.get("files") or []) if f.get("error")]
        if errored_fp_files:
            n = len(errored_fp_files)
            first = errored_fp_files[0]
            more = f" (+{n - 1} more)" if n > 1 else ""
            concerns.append({"level": "error", "kind": "checksum_scan_failed",
                             "text": f"Could not read checksum file {first}{more} — "
                                     f"its tracks could not be verified"})

        # Technical issues are already computed by the engine; surfacing the
        # phase/dead-channel class here means they are visible without opening
        # a tab, which is the whole point of a concerns line.
        for issue in ((r.get("interp") or {}).get("issues") or []):
            concerns.append({"level": "warn", "kind": "technical",
                             "text": f"{issue.get('issue')} — {issue.get('detail')}"})

        # Possible duplicate. Needs performer + year; without both there is
        # nothing meaningful to match on and we say nothing rather than guess.
        if x.get("artist") and x.get("year"):
            try:
                pids = resolve_similar_performer_ids(x["artist"])
                if pids:
                    q = db.session.query(Performance).filter(
                        Performance.performer_id.in_(pids),
                        Performance.start_year == int(x["year"]))
                    if x.get("month"):
                        q = q.filter(Performance.start_month == int(x["month"]))
                    if x.get("day"):
                        q = q.filter(Performance.start_day == int(x["day"]))
                    hits = [p for p in q.all() if p.recordings]
                    if hits:
                        p = hits[0]
                        where = format_partial_date(p.start_year, p.start_month, p.start_day)
                        act   = p.performer.name if p.performer else "this act"
                        more  = f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""
                        concerns.append({
                            "level": "warn", "kind": "duplicate",
                            "text": f"Possible duplicate — library already has "
                                    f"{act} on {where}{more}",
                            "recording_id": p.recordings[0].id,
                        })
            except Exception:  # noqa: BLE001
                _tb.print_exc()   # never let a duplicate check break a card

        r["concerns"] = concerns


# ═════════════════════════════════════════════════════════════════════════════
# Triage
# ═════════════════════════════════════════════════════════════════════════════
@bp.route("/verify-fingerprints", methods=["POST"])
@login_required
def verify_fingerprints():
    """
    POST /api/quality/verify-fingerprints  { "folder_path": "..." }

    The DEEP pass — hashes whole files, so MD5 fingerprints finally get a real
    verdict. Deliberately explicit and per-folder rather than part of triage:
    a 400 MB show means reading 400 MB off the NAS, and doing that for every
    card in a queue would turn a 2-second triage into minutes (Ryan's call,
    2026-08-02). FFP/ST5 already verified for free during triage.

    Synchronous. One folder's audio is a bounded read, and the UI disables the
    button while it runs — a background job here would be more machinery than
    the wait justifies.
    """
    data = request.get_json() or {}
    folder = (data.get("folder_path") or "").strip()
    if not folder or not os.path.isdir(folder):
        return jsonify({"error": f"Folder not found: {folder!r}"}), 400
    if not _within_import_roots(folder):
        return jsonify({"error": "Outside the permitted import roots"}), 403
    try:
        return jsonify(_fingerprint_audit(folder, deep=True))
    except Exception as e:  # noqa: BLE001
        _tb.print_exc()
        return jsonify({"error": str(e)}), 500


def _within_import_roots(path):
    """Same containment rule browse() applies — this endpoint reads audio."""
    roots = [os.path.realpath(r) for r in current_app.config.get("IMPORT_ROOTS", [])]
    real  = os.path.realpath(path)
    return any(real == r or real.startswith(r + os.sep) for r in roots)


@bp.route("/triage", methods=["POST"])
@login_required
def triage():
    """
    POST /api/quality/triage
      { "folder_path": "...", "status": "accepted" | "rejected" | "pending" }
    """
    data = request.get_json() or {}
    folder_path = (data.get("folder_path") or "").strip()
    status = (data.get("status") or "").strip()

    if not folder_path:
        return jsonify({"error": "folder_path is required"}), 400
    try:
        row = qs.set_triage(folder_path, status)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if row is None:
        return jsonify({"error": "No analysis for that folder"}), 404
    return jsonify(qs.serialize(row))


@bp.route("/triage-bulk", methods=["POST"])
@login_required
def triage_bulk():
    """
    POST /api/quality/triage-bulk
      { "folder_paths": [...], "status": "accepted" }

    For "accept everything above N" — the common case on a clean bulk run.
    """
    data = request.get_json() or {}
    paths = data.get("folder_paths") or []
    status = (data.get("status") or "").strip()

    if status not in qs.TRIAGE_STATUSES:
        return jsonify({"error": f"unknown triage status {status!r}"}), 400

    updated = 0
    for p in paths:
        row = qs.get_staging(p)
        if row is not None:
            row.triage_status = status
            updated += 1
    db.session.commit()
    return jsonify({"updated": updated})


@bp.route("/browse", methods=["GET"])
@login_required
def browse():
    """
    List sub-folders of a path so the UI can offer a real directory picker.

    Originally ported from the standalone harness (tools/quality/quality_app.py).
    The in-app picker has since diverged (2026-08-22: the shortcut row and
    "audio" tag were dropped from the UI, and PyWebView's native folder dialog
    — long unused — is now wired to a Browse button), but this endpoint still
    returns `shortcuts` and `audio` for compatibility; nothing client-side
    reads them anymore.

    Read-only, and constrained to IMPORT_ROOTS like every other filesystem
    endpoint here.
    """
    AUDIO_EXT = (".flac", ".mp3", ".wav", ".aiff", ".aif", ".m4a", ".ogg",
                 ".ape", ".wv")

    raw = (request.args.get("path") or "").strip()
    path = os.path.abspath(os.path.expanduser(
        raw or current_app.config.get("IMPORT_DIR", "/")))

    roots = [os.path.realpath(r) for r in current_app.config.get("IMPORT_ROOTS", [])]
    real = os.path.realpath(path)
    # "/" and other ancestors of a root are allowed so the crumbs stay
    # navigable; only descending OUTSIDE every root is refused.
    if not any(real == r or real.startswith(r + os.sep) or r.startswith(real + os.sep)
               or real == "/" for r in roots):
        return jsonify({"error": "Outside the permitted import roots"}), 403
    # WALK UP RATHER THAN FAIL (2026-08-07). A remembered path routinely stops
    # existing: ingesting the last show of an act moves its folder into the
    # library and the empty-parent cleanup removes the act folder behind it. A
    # 400 here stranded the picker on a path it could never load. Climbing to
    # the nearest surviving ancestor puts the user somewhere useful instead,
    # and `redirected_from` lets the UI say why it moved.
    redirected_from = None
    if not os.path.isdir(path):
        redirected_from = path
        probe = os.path.dirname(path)
        while probe and probe != "/" and not os.path.isdir(probe):
            probe = os.path.dirname(probe)
        fallback = current_app.config.get("IMPORT_DIR") or "/"
        path = probe if (probe and os.path.isdir(probe)) else fallback
        if not os.path.isdir(path):
            return jsonify({"error": f"Not a folder: {redirected_from}"}), 400
        # Re-check the roots guard for wherever we landed — climbing must not
        # become a way out of IMPORT_ROOTS.
        real = os.path.realpath(path)
        if not any(real == r or real.startswith(r + os.sep) or r.startswith(real + os.sep)
                   or real == "/" for r in roots):
            return jsonify({"error": "Outside the permitted import roots"}), 403
    try:
        entries = sorted(os.listdir(path), key=lambda s: s.lower())
    except PermissionError:
        return jsonify({"error": f"No permission to read {path}"}), 403

    # A folder holding one recording has no sub-folders at all — just audio
    # plus its FFP/MD5/TXT paperwork sitting right there — and the picker
    # used to have nothing to show for that but "No sub-folders here",
    # which reads as an empty folder rather than the one you're looking at
    # (Ryan, 2026-08-27). List files alongside dirs so landing on that folder
    # shows what's actually in it.
    dirs, files = [], []
    for name in entries:
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        if os.path.isdir(full):
            has_audio, subdirs, subdir_count, size_bytes = _probe_folder(full, AUDIO_EXT)
            dirs.append({"name": name, "path": full,
                         "audio": has_audio, "subdirs": subdirs,
                         "subdir_count": subdir_count, "size_bytes": size_bytes})
        else:
            try:
                size_bytes = os.path.getsize(full)
            except OSError:
                size_bytes = 0
            ext = os.path.splitext(name)[1].lstrip(".").upper()
            files.append({"name": name, "ext": ext, "size_bytes": size_bytes})

    # Bulk Import's standing convention is one folder per act
    # (ImportDir/Performer Name/Show Folder/ — see _cleanup_empty_parent's
    # docstring in utils/ingest.py). Tag every row with whether ITS name
    # already matches a Performer, so the common top-level listing shows
    # which acts are already in the library at a glance. Meaningless at
    # deeper levels (a date-named show folder is never a Performer), but
    # harmless — it just always reads "new" there.
    pstatus = _performer_status_map([d["name"] for d in dirs])
    for d in dirs:
        d["performer_status"] = pstatus[d["name"]]

    parent = os.path.dirname(path.rstrip(os.sep)) or "/"
    return jsonify({
        "path": path,
        # Set when the requested folder had vanished and we climbed to an
        # ancestor — the picker uses it to explain the jump rather than
        # silently showing somewhere else.
        "redirected_from": redirected_from,
        "parent": None if parent == path else parent,
        "dirs": dirs,
        "files": files,
        "here_has_audio": any(e.lower().endswith(AUDIO_EXT) for e in entries),
        "shortcuts": _shortcuts(),
    })


def _performer_status_map(names):
    """
    {folder_name: "existing"|"new"} — same match rule as
    resolve_or_create_performer() and the duplicate-name guard in
    api/performers.py (case-insensitive on Performer.name), so "does this
    performer already exist" means the same thing everywhere in the app.
    Deliberately NOT resolve_or_create_performer itself: that function
    creates on a miss and can fire a synchronous MusicBrainz lookup — fine
    inside the ingest background job it was written for, a landmine if
    called from a read-only endpoint that runs on every folder browse.

    Folder names are NFC-normalised before matching: macOS hands out
    decomposed (NFD) filenames, the DB stores composed (NFC), and comparing
    the two directly is exactly the "Lucía" bug in CONTEXT.md's traps
    section. Batched into one query rather than one per row — a Bulk Import
    root can list a hundred act folders.
    """
    if not names:
        return {}
    normed = {n: unicodedata.normalize("NFC", n).lower() for n in names}
    existing_lower = {
        row[0].lower() for row in
        db.session.query(Performer.name)
                   .filter(func.lower(Performer.name).in_(set(normed.values())))
                   .all()
    }
    return {n: ("existing" if lo in existing_lower else "new")
            for n, lo in normed.items()}


# One entry per probed folder: path → (mtime, has_audio, has_subdirs). Browsing
# is a walk — up, back down, up again — so the same artist folder gets probed
# several times in one sitting, and the answer only changes when the folder
# does. Bounded and cleared wholesale, same as _META_CACHE.
_BROWSE_CACHE = {}
_BROWSE_CACHE_MAX = 4000


def _probe_folder(full, audio_ext):
    """
    (has_audio, has_subdirs, subdir_count, size_bytes) for one folder — the
    facts the picker's row shows.

    Uses os.scandir, NOT os.listdir + os.path.isdir (2026-08-02). scandir's
    DirEntry carries the directory type from the readdir call itself, so
    entry.is_dir() answers from cached d_type with no extra syscall; the old
    code paid a separate stat() per child. On a NAS mount at a few ms per stat,
    an artist folder holding twenty shows cost twenty round trips, and a
    hundred-artist directory cost thousands — which is why Browse sat there
    doing nothing for seconds.

    Still looks ONE level deeper before declaring a folder audio-free: a
    recording that keeps its CD1/CD2 subdirs has no audio at its own root, and
    marking it empty would claim a perfectly analysable show has nothing in it.
    That descent stops at the first subfolder containing audio.

    `size_bytes` is DELIBERATELY SHALLOW — the sum of files sitting directly in
    `full`, not a recursive walk of everything beneath it (Ryan, 2026-08-22).
    A full recursive size is exactly the per-child-stat cost the scandir
    rewrite above exists to avoid; a twenty-show artist folder would pay twenty
    subfolder walks just to paint one row of the picker. One extra stat() per
    direct child (already-cached DirEntry, so no extra syscall to know it's a
    file) is the same order of magnitude as the has_audio/subdirs check this
    function already did, so it rides along for free.
    """
    try:
        mtime = os.stat(full).st_mtime
    except OSError:
        mtime = None
    hit = _BROWSE_CACHE.get(full)
    if hit and hit[0] == mtime:
        return hit[1], hit[2], hit[3], hit[4]

    has_audio, subdir_paths, size_bytes = False, [], 0
    try:
        with os.scandir(full) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                if e.name.lower().endswith(audio_ext):
                    has_audio = True
                try:
                    if e.is_dir():
                        subdir_paths.append(e.path)
                    else:
                        size_bytes += e.stat().st_size
                except OSError:
                    pass
    except (PermissionError, OSError):
        pass

    if not has_audio:
        for sub in subdir_paths:
            try:
                with os.scandir(sub) as it:
                    if any(e.name.lower().endswith(audio_ext) for e in it):
                        has_audio = True
                        break
            except (PermissionError, OSError):
                continue

    subdir_count = len(subdir_paths)
    if len(_BROWSE_CACHE) >= _BROWSE_CACHE_MAX:
        _BROWSE_CACHE.clear()
    _BROWSE_CACHE[full] = (mtime, has_audio, bool(subdir_paths), subdir_count, size_bytes)
    return has_audio, bool(subdir_paths), subdir_count, size_bytes


def _shortcuts():
    """Import folder first, then library, home, all volumes. Deduped."""
    out = []
    for s in (current_app.config.get("IMPORT_DIR"),
              str(current_app.config.get("LIBRARY_ROOT", "")),
              os.path.expanduser("~"), "/Volumes"):
        if s and os.path.isdir(s) and s not in out:
            out.append(s)
    return out


@bp.route("/move", methods=["POST"])
@login_required
def move_out_of_queue():
    """
    POST /api/quality/move
      { "folder_path": "...", "destination": "backlog" | "workshop" }

    Physically moves a show out of the ingest queue — the triage "not this one,
    not now" action.  Because the folder leaves the scanned directory it simply
    stops appearing, with no need for a filter.

    This MOVES USER FILES, so it is deliberately paranoid:
      * destination must be one of the configured TRIAGE_DIRS (no arbitrary
        paths from the client)
      * source must resolve inside an IMPORT_ROOTS entry — the same allowlist
        that guards ingest preview
      * never overwrites: a name collision gets " (2)", " (3)", … rather than
        merging two shows together
      * refuses anything that isn't a directory, and refuses a mount point or
        filesystem root
    """
    data = request.get_json() or {}
    folder_path = (data.get("folder_path") or "").strip()
    destination = (data.get("destination") or "").strip().lower()

    triage_dirs = current_app.config.get("TRIAGE_DIRS", {})
    if destination not in triage_dirs:
        return jsonify({"error": f"Unknown destination {destination!r}; "
                                 f"expected one of {sorted(triage_dirs)}"}), 400

    src = os.path.realpath(folder_path)
    if not os.path.isdir(src):
        return jsonify({"error": f"Not a folder: {folder_path!r}"}), 400
    if os.path.ismount(src) or os.path.dirname(src) == src:
        return jsonify({"error": "Refusing to move a mount point or root"}), 400

    roots = [os.path.realpath(r) for r in current_app.config.get("IMPORT_ROOTS", [])]
    if not any(src == r or src.startswith(r + os.sep) for r in roots):
        return jsonify({"error": "Folder is outside the permitted import roots"}), 403

    dest_root = triage_dirs[destination]
    try:
        os.makedirs(dest_root, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"Cannot create {dest_root}: {e}"}), 500

    base = os.path.basename(src)
    target = os.path.join(dest_root, base)
    n = 2
    while os.path.exists(target):
        target = os.path.join(dest_root, f"{base} ({n})")
        n += 1

    import shutil
    try:
        shutil.move(src, target)
    except OSError as e:
        return jsonify({"error": f"Move failed: {e}"}), 500

    # Keep the analysis, repointed at the new location: re-scanning Backlog or
    # Working later shows the existing score instead of paying to redo it.
    row = qs.get_staging(folder_path)
    if row is not None:
        row.folder_path = qs.norm_path(target)
        row.source_dir = qs.norm_path(dest_root)
        row.triage_status = qs.TRIAGE_REJECTED
        db.session.commit()

    return jsonify({"moved_to": target, "destination": destination})


@bp.route("/staging", methods=["GET"])
@login_required
def staging():
    """
    GET /api/quality/staging?source_dir=...

    Rows for one scanned directory, so returning to the page is instant and a
    restart mid-review loses nothing.
    """
    source_dir = (request.args.get("source_dir") or "").strip()
    if not source_dir:
        return jsonify({"error": "source_dir is required"}), 400
    rows = qs.list_staging(source_dir)
    results = [qs.serialize(r, include_features=True) for r in rows]
    _attach_interpretation(results)
    _attach_metadata(results)
    _attach_fingerprints(results)
    _attach_concerns(results)
    return jsonify({
        "source_dir": qs.norm_path(source_dir),
        "results": results,
    })


@bp.route("/staging/features", methods=["GET"])
@login_required
def staging_features():
    """
    GET /api/quality/staging/features?folder_path=...

    The full raw feature dict for one folder — the Advanced Metrics panel.
    Split out from the list payload because it is large and only wanted on
    expand.
    """
    folder_path = (request.args.get("folder_path") or "").strip()
    row = qs.get_staging(folder_path)
    if row is None:
        return jsonify({"error": "No analysis for that folder"}), 404

    from app.utils.quality import interpret_full
    payload = qs.serialize(row, include_features=True)
    try:
        payload["interpretation"] = interpret_full(payload, payload.get("features") or {})
    except Exception:  # noqa: BLE001
        # Plain-English rendering must never take down the metrics panel.
        _tb.print_exc()
        payload["interpretation"] = None
    return jsonify(payload)


@bp.route("/recording/<int:recording_id>", methods=["GET"])
@login_required
def for_recording(recording_id):
    """Permanent score for one ingested recording — the Fidelity tab."""
    row = qs.get_for_recording(recording_id)
    if row is None:
        return jsonify({"error": "No quality analysis for that recording"}), 404
    include = request.args.get("features") == "1"
    payload = qs.serialize(row, include_features=include)
    # Plain-English interpretation is opt-in with the features dict, because it
    # is derived FROM that dict — asking for one without the other would mean
    # rendering metric rows with no measurements behind them. Mirrors the
    # staging endpoint above deliberately: the View Recording quality pane and
    # the triage card are the same report on the same numbers, so they must be
    # fed from the same code path or they will drift.
    if include:
        from app.utils.quality import interpret_full
        try:
            payload["interpretation"] = interpret_full(payload, payload.get("features") or {})
        except Exception:  # noqa: BLE001
            # Plain-English rendering must never take down the metrics panel.
            _tb.print_exc()
            payload["interpretation"] = None
    return jsonify(payload)
