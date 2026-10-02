"""
app/utils/download_queue.py -- the archive download worker (2026-10-01).

One daemon thread takes the lowest-position queued DownloadJob and fetches its
files into <Downloads>/<identifier>/, one at a time. Shape follows
bulk_ingest_run.py: state lives in DB rows so a restart loses only a progress
bar (resume_on_boot), the thread owns its own app context, and a worker that
decides to leave does so under the same lock start_worker checks, so an
enqueue landing in that window starts a fresh thread instead of trusting one
that is already gone.

Per file: stream to `<name>.part`, verify MD5 (size when the archive gives no
MD5), rename. A file already present and correct is skipped, which makes
retry and resume free. Nothing here ever ingests; downloads stop in the
Downloads folder.

Network: the default FETCHER refuses under TESTING (same rule as
musicbrainz.py). Tests replace FETCHER.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

from flask import current_app

from app.extensions import db
from app.models.download_job import DownloadJob
from app.sources import get_source, SourceError
from app.utils import node_settings
from app.utils.downloads_dir import ensure_downloads_dir, DownloadsDirError

log = logging.getLogger(__name__)

PAUSE_BETWEEN_FILES = 1.0       # courtesy to the archive
PROGRESS_COMMIT_EVERY = 2.0     # seconds between done_bytes writes
MAX_ATTEMPTS = 3                # per file: transient errors and MD5 mismatch
BACKOFF_BASE = 5.0
BACKOFF_CAP = 120.0
_CHUNK = 256 * 1024

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")

_WORKER_LOCK = threading.Lock()
_WORKER = [None]
# Job ids whose cancel was requested while the worker was inside them.
_CANCEL = set()
# The job the worker is inside right now; the delete guard reads it.
_CURRENT = [None]
# Held across the delete guard + rmtree (api/downloads.py) and around the
# worker's makedirs of a job folder, so a delete cannot interleave with an
# enqueue-then-start of the same folder (2026-10-01 review).
FS_LOCK = threading.RLock()


class DownloadError(Exception):
    pass


class ChecksumMismatch(DownloadError):
    pass


class RateLimited(DownloadError):
    def __init__(self, retry_after=None):
        super().__init__("Archive asked us to slow down")
        self.retry_after = retry_after


class _Cancelled(Exception):
    pass


def _sleep(seconds):
    if seconds > 0:
        time.sleep(seconds)


def _now():
    return datetime.now(timezone.utc)


def valid_identifier(ident):
    return bool(ident) and bool(_ID_RE.match(ident))


# -- network -----------------------------------------------------------------

def _http_fetch(url, sink, should_stop):
    """Stream `url` into sink(bytes). Raises RateLimited / DownloadError."""
    try:
        if current_app.config.get("TESTING"):
            raise DownloadError("Network disabled under TESTING")
    except RuntimeError:
        pass
    import requests
    from app.utils.net import USER_AGENT
    try:
        with requests.get(url, stream=True, timeout=(10, 60),
                          headers={"User-Agent": USER_AGENT}) as r:
            if r.status_code in (429, 503):
                ra = r.headers.get("Retry-After")
                try:
                    ra = float(ra) if ra is not None else None
                except ValueError:
                    ra = None
                raise RateLimited(ra)
            if r.status_code != 200:
                raise DownloadError(f"Archive answered {r.status_code}")
            for chunk in r.iter_content(_CHUNK):
                if should_stop():
                    raise _Cancelled()
                if chunk:
                    sink(chunk)
    except requests.RequestException as e:
        raise DownloadError("Connection to the Archive failed") from e


# Replaced in tests: FETCHER(url, sink, should_stop).
FETCHER = _http_fetch


# -- queue operations (called from the API, inside a request) ----------------

def paused():
    return node_settings.download_queue_paused()


def _next_position():
    top = db.session.query(db.func.max(DownloadJob.position)).filter(
        DownloadJob.status == "queued").scalar()
    return (top if top is not None else -1) + 1


def find_open_job(source, source_id):
    return (db.session.query(DownloadJob)
            .filter(DownloadJob.source == source,
                    DownloadJob.source_id == source_id,
                    DownloadJob.status.in_(("queued", "active")))
            .first())


def enqueue(source, source_id, artist, date, venue, total_bytes):
    """Create a queued job and wake the worker. Caller has validated everything
    (known source, valid identifier, not stream only, not already open)."""
    dest = os.path.join(ensure_downloads_dir(), source_id)
    job = DownloadJob(source=source, source_id=source_id, artist=artist,
                      date=date, venue=venue, status="queued",
                      position=_next_position(), total_bytes=total_bytes or 0,
                      done_bytes=0, dest_path=dest)
    db.session.add(job)
    db.session.commit()
    start_worker()
    return job


def cancel(job):
    """queued -> cancelled at once; active -> flagged, the worker cleans up.
    Returns False when the job is not in a cancellable state."""
    # Conditional UPDATE, decided on the row's state at write time: a job that
    # went active a moment ago still matches and is still stopped. The id is
    # always flagged for the same reason; the worker checks the flag between
    # chunks, so the flag is what actually halts an in-flight download.
    was_queued = job.status == "queued"
    n = (db.session.query(DownloadJob)
         .filter(DownloadJob.id == job.id,
                 DownloadJob.status.in_(("queued", "active")))
         .update({"status": "cancelled", "finished_at": _now()},
                 synchronize_session=False))
    db.session.commit()
    if not n:
        db.session.refresh(job)
        return False
    _CANCEL.add(job.id)
    db.session.refresh(job)
    # An active job's folder is removed by the worker as it stops. A queued one
    # has no worker to do it, and may still hold files from an earlier attempt.
    if was_queued:
        _cleanup_cancelled(job.dest_path, job.id)
    return True


def retry(job):
    """failed/cancelled -> queued at the back. Returns False when not retryable."""
    if job.status not in ("failed", "cancelled"):
        return False
    _CANCEL.discard(job.id)       # a stale cancel flag must not kill the rerun
    job.status = "queued"
    job.position = _next_position()
    job.error = None
    job.done_bytes = 0
    job.started_at = None
    job.finished_at = None
    db.session.commit()
    start_worker()
    return True


def remove(job):
    """queued -> row deleted; finished -> cleared (hidden, row kept).
    Returns False for an active job (cancel it first)."""
    if job.status == "active":
        return False
    if job.status == "queued":
        db.session.delete(job)
    else:
        job.status = "cleared"
    db.session.commit()
    return True


def reorder(order):
    """Apply `order` (job ids) to the queued jobs; queued jobs not named keep
    their relative order after the named ones. Ids that are no longer queued
    are ignored -- the list was built from a poll that may be a beat stale."""
    queued = (db.session.query(DownloadJob)
              .filter(DownloadJob.status == "queued")
              .order_by(DownloadJob.position, DownloadJob.id).all())
    by_id = {j.id: j for j in queued}
    seq = []
    for i in order:
        j = by_id.pop(i, None)
        if j is not None:
            seq.append(j)
    seq += [j for j in queued if j.id in by_id]
    for pos, j in enumerate(seq):
        j.position = pos
    db.session.commit()


def set_paused(flag):
    node_settings.set_download_queue_paused(flag)
    if not flag:
        start_worker()


def is_busy_folder(target):
    """True when `target` is the folder of a queued/active job, or of the job
    the worker is inside right now (a just-cancelled job is still being cleaned
    up for a moment). Compared by identity (st_dev, st_ino) when both exist and
    by casefold name otherwise: APFS is case-insensitive, so "SHOW" and "show"
    are one folder and a plain string compare would let the first slip past."""
    rows = (db.session.query(DownloadJob)
            .filter(DownloadJob.status.in_(("queued", "active"))).all())
    cur = _CURRENT[0]
    if cur is not None and all(j.id != cur for j in rows):
        j = db.session.get(DownloadJob, cur)
        if j is not None:
            rows.append(j)
    base = os.path.basename(target.rstrip(os.sep)).casefold()
    for j in rows:
        dest = j.dest_path.rstrip(os.sep)
        if os.path.basename(dest).casefold() == base:
            return True
        try:
            if os.path.samestat(os.stat(target), os.stat(dest)):
                return True
        except OSError:
            pass
    return False


# -- worker ------------------------------------------------------------------

def _pick_next():
    if paused():
        return None
    return (db.session.query(DownloadJob)
            .filter(DownloadJob.status == "queued")
            .order_by(DownloadJob.position, DownloadJob.id).first())


def process_next():
    """Run the next queued job to completion. Returns True if one ran.
    Synchronous; the thread loops on it and tests call it directly."""
    job = _pick_next()
    if job is None:
        return False
    _run_job(job.id)
    return True


def _md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _already_good(target, f):
    if not os.path.isfile(target):
        return False
    if f.get("md5"):
        return _md5_of(target) == f["md5"].lower()
    return bool(f.get("size")) and os.path.getsize(target) == f["size"]


def _safe_target(dest, name):
    """Join an archive-supplied file name under dest, refusing anything that
    would land outside it."""
    if not name or name.startswith(("/", "\\")) or "\x00" in name:
        raise DownloadError("Unsafe file name")
    parts = name.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise DownloadError("Unsafe file name")
    target = os.path.join(dest, *parts)
    if os.path.commonpath([os.path.realpath(dest), os.path.realpath(target)]) != os.path.realpath(dest):
        raise DownloadError("Unsafe file name")
    return target


def _fetch_one(job_id, f, target, progress):
    """Fetch one file with retries. progress(n) is called per chunk.

    Returns True when the file is verified (MD5 matched) or unverifiable (the
    archive gave no MD5 and the size matched), False when the MD5 still
    mismatched after every attempt. A mismatched file is KEPT under its final
    name (Ryan, 2026-10-01): the download is complete, the checksum is not, and
    the Downloads page says so. Network/HTTP failures raise instead."""
    part = target + ".part"
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        h = hashlib.md5()
        written = [0]
        try:
            with open(part, "wb") as fh:
                def sink(chunk):
                    fh.write(chunk)
                    h.update(chunk)
                    written[0] += len(chunk)
                    progress(len(chunk))
                FETCHER(f["url"], sink, lambda: job_id in _CANCEL)
            if f.get("md5") and h.hexdigest() != f["md5"].lower():
                if attempt == MAX_ATTEMPTS:
                    os.replace(part, target)
                    return False
                raise ChecksumMismatch(f"Checksum mismatch: {os.path.basename(f['name'])}")
            if not f.get("md5") and f.get("size") and written[0] != f["size"]:
                raise DownloadError(f"Incomplete file: {os.path.basename(f['name'])}")
            os.replace(part, target)
            return True
        except RateLimited as e:
            last = e
            progress(-written[0])
            _sleep(min(e.retry_after if e.retry_after is not None
                       else BACKOFF_BASE * attempt, BACKOFF_CAP))
        except DownloadError as e:
            last = e
            progress(-written[0])
            _sleep(min(BACKOFF_BASE * attempt, BACKOFF_CAP))
        finally:
            # Success renamed it away; anything else leaves a partial file
            # that must not be mistaken for a download.
            try:
                os.remove(part)
            except OSError:
                pass
    raise last


def _run_job(job_id):
    # Claim with a conditional UPDATE so a cancel that lands first wins cleanly.
    claimed = (db.session.query(DownloadJob)
               .filter(DownloadJob.id == job_id, DownloadJob.status == "queued")
               .update({"status": "active", "started_at": _now(), "error": None,
                        "done_bytes": 0}, synchronize_session=False))
    db.session.commit()
    if not claimed:
        return
    job = db.session.get(DownloadJob, job_id)
    _CURRENT[0] = job_id
    dest = job.dest_path
    created = False
    try:
        # Resolve the destination NOW, not from the path stored at enqueue: the
        # Downloads folder can change between the two (Settings, or a boot where
        # config was patched after the row was written).
        if not valid_identifier(job.source_id):
            raise DownloadError("Bad item id")
        dest = os.path.join(ensure_downloads_dir(), job.source_id)
        job.dest_path = dest
        db.session.commit()
        src = get_source(job.source)
        if src is None:
            raise DownloadError("Unknown source")
        plan = src.download_plan(job.source_id)
        if not plan:
            raise DownloadError("Nothing to download")
        job.total_bytes = sum(f.get("size") or 0 for f in plan)
        db.session.commit()

        with FS_LOCK:
            created = not os.path.isdir(dest)
            os.makedirs(dest, exist_ok=True)

        state = {"done": 0, "last": time.monotonic()}

        def commit_progress(force=False):
            now = time.monotonic()
            if force or now - state["last"] >= PROGRESS_COMMIT_EVERY:
                j = db.session.get(DownloadJob, job_id)
                if j is not None and j.status == "active":
                    j.done_bytes = max(0, state["done"])
                    db.session.commit()
                state["last"] = now

        def progress(n):
            state["done"] += n
            commit_progress()

        verified = 0
        mismatched = []
        for i, f in enumerate(plan):
            if job_id in _CANCEL:
                raise _Cancelled()
            target = _safe_target(dest, f["name"])
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if _already_good(target, f):
                state["done"] += f.get("size") or os.path.getsize(target)
                commit_progress(force=True)
                verified += 1 if f.get("md5") else 0
                continue
            ok = _fetch_one(job_id, f, target, progress)
            if not ok:
                mismatched.append(os.path.basename(f["name"]))
            elif f.get("md5"):
                verified += 1
            commit_progress(force=True)
            if i < len(plan) - 1:
                _sleep(PAUSE_BETWEEN_FILES)

        j = db.session.get(DownloadJob, job_id)
        if j is not None and j.status == "active":
            j.status = "done"
            j.done_bytes = j.total_bytes
            j.finished_at = _now()
            j.checksums = json.dumps({"verified": verified, "mismatched": mismatched})
            if mismatched:
                j.error = (f"Checksum mismatch: {len(mismatched)} "
                           f"file{'s' if len(mismatched) != 1 else ''} "
                           f"({', '.join(mismatched)})")[:1000]
            db.session.commit()
    except _Cancelled:
        _cleanup_cancelled(dest, job_id)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        if job_id in _CANCEL:
            _cleanup_cancelled(dest, job_id)
        else:
            _strip_parts(dest)
            try:
                j = db.session.get(DownloadJob, job_id)
                if j is not None and j.status == "active":
                    j.status = "failed"
                    j.error = _short(e)
                    j.finished_at = _now()
                    db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                log.exception("could not record failure of download job %s", job_id)
            if not isinstance(e, (DownloadError, SourceError, DownloadsDirError)):
                log.exception("download job %s crashed", job_id)
    finally:
        if job_id in _CANCEL:
            _cleanup_cancelled(dest, job_id)
        _CANCEL.discard(job_id)
        _CURRENT[0] = None


def _short(e):
    msg = str(e).strip() or e.__class__.__name__
    if isinstance(e, (DownloadError, SourceError)):
        return msg[:300]
    if isinstance(e, (OSError, DownloadsDirError)):
        return ("Could not write to the Downloads folder: " + msg)[:300]
    return "Download failed"


def _strip_parts(dest):
    for root, _dirs, files in os.walk(dest):
        for n in files:
            if n.endswith(".part"):
                try:
                    os.remove(os.path.join(root, n))
                except OSError:
                    pass


def _has_complete_download(dest, exclude_id):
    """True when an earlier job for this folder finished `done` -- including one
    the user has since cleared from the panel (a cleared job keeps its byte
    counts, and only a job that finished has done_bytes >= total_bytes > 0)."""
    rows = (db.session.query(DownloadJob)
            .filter(DownloadJob.dest_path == dest, DownloadJob.id != exclude_id,
                    DownloadJob.status.in_(("done", "cleared"))).all())
    return any(j.status == "done" or (j.total_bytes and j.done_bytes >= j.total_bytes)
               for j in rows)


def _cleanup_cancelled(dest, job_id):
    """
    A cancel removes everything incomplete (Ryan, 2026-10-01): the job's whole
    folder, files that had finished included. The one exception is a folder an
    earlier job already completed, which a cancelled re-download must never
    destroy -- there only the .part files go.

    This used to delete the folder only when THIS run had created it, so a
    folder that already existed (a restart-resume, a retry after a failure, a
    second attempt) survived every cancel with its partial files in it.
    """
    with FS_LOCK:
        if not os.path.isdir(dest):
            return
        _strip_parts(dest)
        if not _has_complete_download(dest, job_id):
            shutil.rmtree(dest, ignore_errors=True)


def start_worker(app=None):
    """Start the thread unless one is alive. Idempotent."""
    if app is None:
        app = current_app._get_current_object()

    def _work():
        with app.app_context():
            try:
                while True:
                    with _WORKER_LOCK:
                        if _pick_next() is None:
                            _WORKER[0] = None
                            return
                    process_next()
            except Exception:  # noqa: BLE001
                log.exception("download worker stopped")
                db.session.rollback()
                # Never leave a job `active` with nobody driving it.
                try:
                    (db.session.query(DownloadJob)
                     .filter(DownloadJob.status == "active")
                     .update({"status": "queued"}))
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                with _WORKER_LOCK:
                    _WORKER[0] = None

    with _WORKER_LOCK:
        t = _WORKER[0]
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=_work, daemon=True, name="download-queue")
        _WORKER[0] = t
        t.start()


def resume_on_boot(app):
    """
    Called from run.py (after the library/Downloads config is patched and the
    schema exists) and from run_headless.py -- NOT from create_app(), whose
    config is not final yet (2026-10-01 review). Never in SERVER_MODE. A job
    left `active` when the process stopped goes back to `queued` -- its
    finished files are skipped by MD5 on the next pass -- and the worker starts
    if anything is waiting. A failed query just means there is nothing to
    resume; it must never stop boot.
    """
    with app.app_context():
        try:
            n = (db.session.query(DownloadJob)
                 .filter(DownloadJob.status == "active")
                 .update({"status": "queued"}))
            db.session.commit()
            waiting = (db.session.query(DownloadJob.id)
                       .filter(DownloadJob.status == "queued").first())
        except Exception:  # noqa: BLE001
            db.session.rollback()
            return
        if waiting and not paused() and not app.config.get("TESTING"):
            start_worker(app)
