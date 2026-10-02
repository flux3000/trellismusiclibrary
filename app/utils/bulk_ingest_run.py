"""
app/utils/bulk_ingest_run.py -- Bulk Ingest worker (spec chunk 5).

A run walks LIBRARY_ROOT once (discover), then works through every show-
shaped folder it found (process), turning each into either a Recording
(ingested), a QualityAnalysis review row (a human decision is needed first),
or a skipped/failed BulkIngestItem. Both the walk and the work are driven off
BulkIngestItem rows in the DB rather than in-memory state, so an app restart
mid-run loses nothing but a progress bar (resume_on_boot below).

Every source folder here is already inside LIBRARY_ROOT -- this is bulk_ingest
of an existing collection, not ingest of new material -- so _do_confirm's
in-root-source path applies unconditionally: nothing is ever moved, renamed,
flattened or deduped on disk. A folder that becomes a Recording keeps
sitting exactly where discover() found it, and its Recording.folder_path
ends up equal to the BulkIngestItem's own rel_path (see move_to_library's
"in-root bulk_ingest" note in app/utils/ingest.py). That equality is what lets
discover() recognise "already in the library" and what lets process() find
a just-ingested item's own fingerprints back out of RecordingFingerprint by
recording_id, without having to re-derive a path.
"""

import json
import os
import threading
import time
import traceback
import unicodedata

from app.extensions import db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.recording import Recording, RecordingFingerprint
from app.models.quality import QualityAnalysis
from app.models.user import User
from app.utils.ingest import resolve_shows_in_dir
from app.utils.health import compute_health
from app.utils.checksums import parse_checksum_file
from app.utils.format import format_partial_date
from app.utils.resolve import DEDUP_FP_TYPES

# One worker thread per process, same shape as _analysis_worker/_ANALYSIS_Q
# in app/api/ingest.py: a module-level flag under a lock rather than a
# thread object, since all this needs to know is "has one been started."
_WORKER_LOCK = threading.Lock()
# run_id -> Thread, protected by _WORKER_LOCK. Lets _start_worker refuse to
# start a second worker for a run that already has one alive (S3/S9):
# Pause then Resume within one item used to always start a fresh thread,
# so the old thread (still inside its slow _do_confirm call) and the new
# one both worked the same run's items at once.
_ACTIVE_WORKERS = {}

# Canonical DEDUP_FP_TYPES now lives in app.utils.resolve (spec section 6).
# "md5" is deliberately excluded there too: two different recordings of the
# same show do not share MD5s (that hash covers the whole file, encoding
# included), so it would never fire, and computing it here would mean the
# exact per-file re-hash chunk 5 spends effort NOT doing during ingest (see
# _do_confirm's bulk=True skip of MD5 verification) just to build a map that
# gains nothing from it.
_DEDUP_FP_TYPES = DEDUP_FP_TYPES


def _norm_rel(path):
    """NFC-normalised, forward-slash, no leading/trailing slash -- the same
    key shape BulkIngestItem.rel_path and (for an in-root source) Recording.
    folder_path both use."""
    p = unicodedata.normalize("NFC", str(path)).replace(os.sep, "/")
    return p.strip("/")


def _on_disk_path(root, rel):
    """
    The path to actually scan/open on disk for an BulkIngestItem's NFC
    rel_path (R2-2).

    rel_path is deliberately NFC-normalised -- it is the dedup key shared
    with Recording.folder_path -- but the bytes a folder was created or
    copied with are not always NFC (an rsync'd tree from an HFS+-era
    machine, or simply a byte-exact filesystem like the ext4 this was
    reproduced on). join(root, rel) with the NFC string then does not
    exist, and the folder reads as empty of audio. Tries the direct join
    first (the overwhelmingly common case, and free), and only when that
    misses walks each path component looking for the on-disk name whose own
    NFC form matches -- never changing rel_path itself, only what gets
    opened.
    """
    direct = os.path.join(root, rel)
    if os.path.exists(direct):
        return direct
    cur = root
    for part in rel.split("/"):
        if not part:
            continue
        want = unicodedata.normalize("NFC", part)
        match = None
        try:
            for name in os.listdir(cur):
                if unicodedata.normalize("NFC", name) == want:
                    match = name
                    break
        except OSError:
            return direct
        cur = os.path.join(cur, match if match is not None else part)
    return cur


def _owner_user_id():
    """
    The install's owner, for the user_id a bulk-confirmed recording's
    RecordingEvent row needs. There is no browser session in a background
    worker to take it from, so this uses the same query create_app's
    DEV_MODE/SINGLE_USER_DESKTOP auto-login already treats as "the owner":
    the first active admin.
    """
    user = db.session.query(User).filter_by(role="admin", is_active=True).first()
    return user.id if user else None


def _active_run():
    """The run still in flight, if any -- running or paused, most recent
    first. One at a time: a second start_run() while one is active returns
    this one rather than starting a competing walk of the same tree."""
    return (db.session.query(BulkIngestRun)
            .filter(BulkIngestRun.status.in_(("running", "paused")))
            .order_by(BulkIngestRun.id.desc())
            .first())


def _ingested_rel_paths_all_runs():
    """
    Every rel_path any earlier run already turned into a Recording, across
    ALL runs -- not just the most recent one. Re-run's discover() uses this
    so a folder ingested two runs ago is never re-offered even though its own
    run finished long since (spec 5b "Re-run").
    """
    rows = (db.session.query(BulkIngestItem.rel_path)
            .filter(BulkIngestItem.status == "ingested")
            .all())
    return {_norm_rel(r) for (r,) in rows}


def discover(run):
    """
    Walk run.root once, using the same show-folder test resolve_shows_in_dir
    (app/utils/ingest.py) uses for batch scanning and Listening Quality
    triage -- so Bulk Ingest can never disagree with either about what
    counts as a show. Inserts one pending BulkIngestItem per candidate folder,
    committing every 200 rows so a very large library can't lose an entire
    walk to one late failure.

    Idempotent: an existing (run_id, rel_path) row (the unique constraint on
    BulkIngestItem) is left alone, so calling this again on the same run only
    adds folders that are genuinely new since the last call.
    """
    root = run.root

    # A 'review' item (this run's or an earlier one's) may have been
    # accepted through Review & Ingest since it was written -- reconcile it
    # to 'ingested' BEFORE building already_ingested, so that folder's now-
    # existing Recording is recognised below instead of getting a second,
    # duplicate item ('skipped/already_in_library') (S4b). Every run, not
    # just this one: the folder could have been left in 'review' by a run
    # that finished long ago.
    _reconcile_review_items(run=None)

    already_ingested = _ingested_rel_paths_all_runs()
    existing_folder_paths = {
        _norm_rel(fp) for (fp,) in
        db.session.query(Recording.folder_path).filter(Recording.folder_path.isnot(None)).all()
    }
    existing_items = {
        r for (r,) in
        db.session.query(BulkIngestItem.rel_path).filter(BulkIngestItem.run_id == run.id).all()
    }

    unreadable_paths = []
    show_paths = resolve_shows_in_dir(root, unreadable=unreadable_paths)

    pending_writes = 0
    for abs_path in show_paths:
        rel = _norm_rel(os.path.relpath(abs_path, root))
        if rel in existing_items:
            continue
        if rel in already_ingested:
            # Ingested by an earlier run -- never re-offered, even on a fresh
            # run over the same root (spec 5b "Re-run").
            continue
        if rel in existing_folder_paths:
            db.session.add(BulkIngestItem(run_id=run.id, rel_path=rel,
                                        status="skipped", reason="already_in_library"))
        else:
            db.session.add(BulkIngestItem(run_id=run.id, rel_path=rel, status="pending"))
        existing_items.add(rel)
        pending_writes += 1
        if pending_writes % 200 == 0:
            db.session.commit()

    # S8: a top-level folder the app cannot even list (chmod 000, an NFS
    # mount that dropped mid-walk) must be reported as failed/unreadable,
    # never silently dropped the way resolve_shows_in_dir drops a folder
    # that is merely empty of audio.
    for abs_path, message in unreadable_paths:
        rel = _norm_rel(os.path.relpath(abs_path, root))
        if rel in existing_items:
            continue
        db.session.add(BulkIngestItem(run_id=run.id, rel_path=rel,
                                    status="failed", reason="unreadable", detail=message))
        existing_items.add(rel)
        pending_writes += 1
        if pending_writes % 200 == 0:
            db.session.commit()
    db.session.commit()


def _build_dedup_map():
    """
    {checksum_hex: recording_id} from every FFP/ST5 fingerprint already on
    disk (see _DEDUP_FP_TYPES), across every Recording -- not just ones this
    run touches. Rebuilt at the start of every process() call (including on
    resume) rather than cached, since it must reflect whatever the library
    already held before this run started.
    """
    hash_to_recording = {}
    rows = (db.session.query(RecordingFingerprint.recording_id, RecordingFingerprint.content)
            .filter(RecordingFingerprint.fingerprint_type.in_(_DEDUP_FP_TYPES))
            .all())
    for recording_id, content in rows:
        for entry in parse_checksum_file(content or ""):
            hash_to_recording.setdefault(entry["checksum"], recording_id)
    return hash_to_recording


def _record_hashes(recording_id, hash_to_recording):
    """
    After an item is ingested: fold its own FFP/ST5 hashes into the run's
    per-run cache, so a LATER duplicate in the same run is caught by
    find_duplicates()'s hash_cache lookup without a fresh DB query per item.

    Exact-duplicate detection itself moved BEFORE ingest (spec section 9,
    resolved question 1: find_duplicates() checks the scan's own fingerprint
    files against this cache/DB, and verdict() skips the item outright) --
    this function only keeps the cache current for what comes next.
    """
    rows = (db.session.query(RecordingFingerprint.content)
            .filter(RecordingFingerprint.recording_id == recording_id,
                    RecordingFingerprint.fingerprint_type.in_(_DEDUP_FP_TYPES))
            .all())
    for (content,) in rows:
        for entry in parse_checksum_file(content or ""):
            hash_to_recording.setdefault(entry["checksum"], recording_id)


def process(run, stop_flag):
    """
    Work every 'pending' BulkIngestItem on `run`, in id order.

    stop_flag: callable() -> bool, polled between items. Pause and resume
    both work by flipping run.status and letting the caller keep calling (or
    stop calling) this function -- it never loops internally waiting to be
    resumed, so a paused run simply has nobody driving it forward.

    Never raises out of the loop: a single folder's exception is caught,
    recorded on that item as 'failed', and the run moves on -- one bad
    folder must not stop a whole-library pass.
    """
    from app.api.ingest import auto_confirm

    library_root = run.root
    user_id = _owner_user_id()
    hash_to_recording = _build_dedup_map()

    while True:
        if stop_flag():
            return
        item = (db.session.query(BulkIngestItem)
                .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "pending")
                .order_by(BulkIngestItem.id.asc())
                .first())
        if item is None:
            break

        item.status = "in_progress"
        db.session.commit()

        # R2-2: item.rel_path is deliberately NFC (the dedup key), but the
        # bytes actually on disk can be NFD -- resolve the real path to
        # scan/open rather than the one that merely LOOKS right.
        folder_abs = _on_disk_path(library_root, item.rel_path)

        try:
            # A folder that became a Recording under a DIFFERENT rel_path
            # between discover() and now (e.g. a resumed item whose folder
            # was ingested some other way in the meantime) is simply not
            # re-ingested.
            already = (db.session.query(Recording.id)
                      .filter(Recording.folder_path == item.rel_path)
                      .first())
            if already:
                item.status = "skipped"
                item.reason = "already_in_library"
                db.session.commit()
                continue

            # The one server function that turns a folder into an ingested
            # (or reviewed, or skipped) recording, via the resolver -- Batch
            # Import's auto-confirm endpoint calls the exact same function
            # (spec section 4), so a bulk-ingested and an auto-ingested-from-
            # Batch-Import recording can never come out different.
            outcome = auto_confirm(folder_abs, user_id, bulk=True,
                                   hash_cache=hash_to_recording)
            status   = outcome["status"]
            reasons  = outcome["reasons"]
            resolved = outcome["resolved"]
            item.format = outcome.get("format")

            # meta_band (2026-09-27 unified ingest queue table): the same
            # High/Medium/Low read compute_health() gives triage, computed
            # here at extraction time -- cheaper than a per-row recompute at
            # serialization, and needs no schema change (it rides inside the
            # existing `meta` JSON blob rather than a new column). A row
            # that needs review is always "red" regardless of the computed
            # band, same rule the shared table applies everywhere else.
            meta_band = "red"
            if resolved is not None and resolved.scan:
                try:
                    meta_band = compute_health(resolved.scan)["band"]
                except Exception:
                    meta_band = "red"
            if status == "review":
                meta_band = "red"

            date = (resolved.date.value if resolved else None) or {}
            # 2026-09-27 progress/log redesign: the log's expand panel needs
            # these fields without a per-item re-scan -- stash whatever the
            # resolver found, win or lose (a review/failed item still has
            # partial data worth showing).
            item.meta = json.dumps({
                "artist":    resolved.artist.value if resolved else None,
                "date_text": format_partial_date(date.get("year"), date.get("month"),
                                                 date.get("day")),
                "venue":     resolved.venue.value if resolved else None,
                "city":      resolved.city.value if resolved else None,
                "state":     resolved.state.value if resolved else None,
                "country":   resolved.country.value if resolved else None,
                "source":    resolved.source.value if resolved else None,
                "lineage":   resolved.lineage.value if resolved else None,
                "title":     resolved.album.value if resolved else None,
                "meta_band": meta_band,
            })

            # BulkIngestItem.reason is a single String(32) column -- there is
            # no list-of-reasons column to add without a migration framework
            # (out of scope here), so several reasons are stored comma-joined
            # and split back apart by the one reader (app/api/quality.py's
            # _bulk_ingest_review_reasons).
            reason_str = ",".join(reasons) if reasons else None

            if status == "ingested":
                recording_id = outcome["result"]["recording_id"]
                item.status = "ingested"
                item.reason = None
                item.kind = resolved.kind if resolved else None
                item.recording_id = recording_id
                _record_hashes(recording_id, hash_to_recording)
            elif status == "review":
                item.status = "review"
                item.reason = reason_str
                item.kind = resolved.kind if resolved else None
                _write_review_staging(folder_abs, library_root, item)
            elif status == "skipped":
                # Exact content duplicate (spec section 9, resolved
                # question 1) -- never ingested a second time.
                item.status = "skipped"
                item.reason = reason_str or "duplicate_content"
                item.duplicate_of = outcome.get("duplicate_of")
            else:  # "failed" -- no_audio / unreadable
                item.status = "failed"
                item.reason = reason_str
                # R2-N2: detail is supposed to be a human-readable message,
                # not the reason word itself.
                item.detail = outcome.get("detail") or reason_str
            db.session.commit()
        except OSError as e:
            # R2-1: a folder that became unreadable between discover() and
            # here (an NFS mount that dropped, a chmod that landed mid-run)
            # is reported the same way discover()'s own top-level check
            # reports one, not folded into the generic 'failed' bucket
            # below with no reason at all.
            db.session.rollback()
            item = db.session.get(BulkIngestItem, item.id)
            item.status = "failed"
            item.reason = "unreadable"
            item.detail = str(e)
            db.session.commit()
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            item = db.session.get(BulkIngestItem, item.id)
            item.status = "failed"
            item.detail = str(e)
            db.session.commit()
            traceback.print_exc()

    # No pending items left -- the run is done, unless it was paused out from
    # under us between the last item and this check.
    run = db.session.get(BulkIngestRun, run.id)
    if run.status != "running":
        return
    remaining = (db.session.query(BulkIngestItem.id)
                .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "pending")
                .first())
    if remaining:
        return
    run.status = "done"
    from datetime import datetime, timezone
    run.finished_at = datetime.now(timezone.utc)
    db.session.commit()

    # Chunk 6 adds the follow-up queue (non-music signal / analysis / tag
    # write for bulk-ingested recordings). Not built yet in this chunk --
    # guarded so this module never hard-depends on it.
    try:
        from app.utils.bulk_ingest_followup import enqueue_followups
        enqueue_followups(run)
    except ImportError:
        pass


def _write_review_staging(folder_abs, library_root, item):
    """
    A 'review' item gets a QualityAnalysis staging row (app/models/quality.py)
    so it shows up in the existing triage/review surfaces rather than only
    living in BulkIngestItem -- app/api/quality.py's _attach_concerns() (chunk
    5c) matches back to this item by folder_path.
    """
    from app.utils.quality_store import norm_path

    folder_path = norm_path(folder_abs)
    row = (db.session.query(QualityAnalysis)
           .filter(QualityAnalysis.folder_path == folder_path)
           .first())
    is_new = row is None
    if is_new:
        row = QualityAnalysis(folder_path=folder_path)
        db.session.add(row)
    # source_dir/name are always refreshed -- they just describe where the
    # folder is, not a human decision. Scores and triage_status are a paid
    # analysis and a human's call: only set them on a brand-new row, never
    # null or reset them on a re-run that finds the row already there
    # (S2 -- CONTEXT: research the human paid for is never discarded).
    row.source_dir = norm_path(library_root)
    row.name = os.path.basename(folder_abs.rstrip("/\\"))
    if is_new:
        row.triage_status = "pending"
        row.listening_quality = None
        row.score_tone = None
        row.score_noise = None
        row.score_dynamics = None
        row.error = None
    db.session.flush()


def _reconcile_review_items(run=None):
    """
    S4b: a 'review' BulkIngestItem whose QualityAnalysis staging row has since
    been accepted through Review & Ingest (not through this run's own
    process()) already has a Recording -- promote_to_recording backlinked
    staging.recording_id when it was promoted, but nothing told the
    BulkIngestItem itself. Left alone, the item stays 'review' forever, and a
    later discover() would insert a SECOND item for the same folder
    ('skipped/already_in_library'), instead of recognising the first one as
    done.

    Matches by folder_path (the same key _write_review_staging used to file
    the staging row in the first place). `run` restricts the BulkIngestItem
    side to one run; None (resume_on_boot / a bare enqueue_followups() call)
    reconciles every run's review items.
    """
    from app.utils.quality_store import norm_path

    q = (db.session.query(BulkIngestItem, BulkIngestRun)
         .join(BulkIngestRun, BulkIngestRun.id == BulkIngestItem.run_id)
         .filter(BulkIngestItem.status == "review"))
    if run is not None:
        q = q.filter(BulkIngestItem.run_id == run.id)

    reconciled = 0
    for item, item_run in q.all():
        folder_path = norm_path(os.path.join(item_run.root, item.rel_path))
        staging = (db.session.query(QualityAnalysis)
                   .filter(QualityAnalysis.folder_path == folder_path)
                   .first())
        if staging is None:
            continue
        if staging.recording_id is not None:
            item.status = "ingested"
            item.reason = None
            item.recording_id = staging.recording_id
            reconciled += 1
        elif staging.triage_status == "rejected":
            # R2-N6: a review row the reviewer rejected (not accepted) never
            # gets a recording_id, so it used to stay 'review' forever --
            # counted under Need Review even though the review list itself
            # (which excludes rejected rows) no longer shows it. 'skipped'
            # matches what actually happened to it.
            item.status = "skipped"
            item.reason = "rejected"
            reconciled += 1
    if reconciled:
        db.session.commit()
    return reconciled


def _start_worker(run_id, app=None):
    if app is None:
        from flask import current_app
        app = current_app._get_current_object()

    def _stop_flag():
        with app.app_context():
            r = db.session.get(BulkIngestRun, run_id)
            return r is None or r.status != "running"

    def _leaving(run_id):
        """
        R2-3: called whenever this worker is about to stop because the run
        no longer reads 'running' -- right after process() returns (paused
        mid-item) or when the top-of-loop check finds it so. The thread is
        still `is_alive()` for as long as this function's caller takes to
        actually return (context-teardown, GC, whatever the interpreter is
        doing), and a Resume landing in that window used to see an "alive"
        worker and refuse to start a new one, even though this one had
        already committed to leaving.

        Pops this thread from _ACTIVE_WORKERS and re-reads status under the
        SAME lock _start_worker's own is_alive() check uses, so a concurrent
        Resume either lands before this runs (sees 'running', the loop below
        just continues) or after the pop (nothing looks alive, so a fresh
        worker starts). Returns True when the worker should actually stop;
        False means the run went back to 'running' while we were mid-exit,
        and this same thread re-registers itself and keeps going instead of
        leaving a run with no one driving it.
        """
        with _WORKER_LOCK:
            _ACTIVE_WORKERS.pop(run_id, None)
            r = db.session.get(BulkIngestRun, run_id)
            if r is not None and r.status == "running":
                _ACTIVE_WORKERS[run_id] = threading.current_thread()
                return False
            return True

    def _work():
        with app.app_context():
            r = db.session.get(BulkIngestRun, run_id)
            if r is None:
                return
            try:
                discover(r)
                # process() returns as soon as stop_flag() is True (paused),
                # so a resume needs a NEW call to process() -- see resume().
                while True:
                    r = db.session.get(BulkIngestRun, run_id)
                    if r is None:
                        with _WORKER_LOCK:
                            _ACTIVE_WORKERS.pop(run_id, None)
                        return
                    if r.status != "running":
                        if _leaving(run_id):
                            return
                        continue
                    process(r, _stop_flag)
                    if _leaving(run_id):
                        return
                    remaining = (db.session.query(BulkIngestItem.id)
                                .filter(BulkIngestItem.run_id == run_id,
                                        BulkIngestItem.status == "pending")
                                .first())
                    if not remaining:
                        with _WORKER_LOCK:
                            _ACTIVE_WORKERS.pop(run_id, None)
                        return
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                with _WORKER_LOCK:
                    _ACTIVE_WORKERS.pop(run_id, None)
                r = db.session.get(BulkIngestRun, run_id)
                if r is not None:
                    # R2-N5: leave the run 'paused', not 'running', so the
                    # page offers Resume instead of showing a run nobody is
                    # driving and polling forever with no worker to catch up
                    # to.
                    r.last_error = traceback.format_exc()
                    r.status = "paused"
                    db.session.commit()

    with _WORKER_LOCK:
        existing = _ACTIVE_WORKERS.get(run_id)
        if existing is not None and existing.is_alive():
            # A worker for this run is already running (e.g. Resume called
            # while the paused run's own thread was still mid-item) --
            # never start a second one on the same run.
            return
        t = threading.Thread(target=_work, daemon=True)
        _ACTIVE_WORKERS[run_id] = t
        t.start()


def start_run(library_root):
    """
    Return the active run (running or paused) if one exists, else create one
    (status running) and start its worker thread.
    """
    run = _active_run()
    if run:
        return run
    run = BulkIngestRun(root=library_root, status="running")
    db.session.add(run)
    db.session.commit()
    _start_worker(run.id)
    return run


def pause_run(run):
    run.status = "paused"
    db.session.commit()


def resume_run(run):
    run.status = "running"
    db.session.commit()
    _start_worker(run.id)


def reset_in_progress(run):
    """
    Any item still 'in_progress' on `run` was mid-bulk_ingest at whatever moment
    stopped the worker (an app restart, a simulated crash in a test) and its
    outcome is unknown -- _do_confirm's move is not safely resumable
    partway through, so it goes back to 'pending' to be tried again from
    scratch. Split out from resume_on_boot() so a test can exercise exactly
    this reset without needing a live app/thread around it.
    """
    (db.session.query(BulkIngestItem)
     .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "in_progress")
     .update({"status": "pending"}, synchronize_session=False))
    db.session.commit()


def resume_on_boot(app):
    """
    Called from create_app (never in SERVER_MODE): if a run was left
    'running' when the process last stopped, reset its in-progress item (see
    reset_in_progress) and restart its worker. A run that was 'paused' is
    left exactly as it was; only the caller pressing Resume restarts its
    worker.

    create_app() runs before the schema exists on a genuinely fresh install
    (run.py's first_run_setup() -- db.create_all() -- runs AFTER create_app()
    returns) and before a temp test DB has been through db.create_all() in
    conftest's app fixture. Either way there is provably no run to resume,
    so any failure querying bulk_ingest_run is swallowed exactly like
    ensure_install_epoch() swallows its own boot-time filesystem misses --
    this must never be able to stop the app from starting.
    """
    with app.app_context():
        try:
            run = (db.session.query(BulkIngestRun)
                  .filter(BulkIngestRun.status == "running")
                  .order_by(BulkIngestRun.id.desc())
                  .first())
        except Exception:  # noqa: BLE001
            db.session.rollback()
            return
        if run is not None:
            reset_in_progress(run)
            run_id = run.id
            _start_worker(run_id, app=app)

        # Chunk 6: pending follow-up work (scoring, MusicBrainz lookups) must
        # survive a process restart too, not just a running run's own worker.
        # Only bothers if Bulk Ingest has ever run at all -- guarded and
        # swallowed exactly like the query above, for the same fresh-install /
        # pre-db.create_all() reason.
        try:
            has_any_run = db.session.query(BulkIngestRun.id).first() is not None
        except Exception:  # noqa: BLE001
            db.session.rollback()
            return
        if has_any_run:
            try:
                from app.utils.bulk_ingest_followup import enqueue_followups
                enqueue_followups()
            except ImportError:
                pass
            except Exception:  # noqa: BLE001
                db.session.rollback()
