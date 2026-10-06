"""
app/utils/bulk_ingest_run.py -- Bulk Ingest worker (spec chunk 5).

A run walks LIBRARY_ROOT once (discover), then works through every show-
shaped folder it found (process), turning each into either a Recording
(ingested), a QualityAnalysis review row (a human decision is needed first),
or a skipped/failed BulkIngestItem. Both the walk and the work are driven off
BulkIngestItem rows in the DB rather than in-memory state, so an app restart
mid-run loses nothing but a progress bar (resume_on_boot below).

Placement is derived from where the run's root sits, never chosen:

  * a root equal to or inside LIBRARY_ROOT is cataloged IN PLACE --
    _do_confirm's in-root-source path applies: nothing is ever moved,
    renamed, flattened, deduped on disk or retagged. A folder that becomes a
    Recording keeps sitting exactly where discover() found it, and its
    Recording.folder_path ends up equal to the BulkIngestItem's own rel_path
    (see move_to_library's "in-root bulk_ingest" note in
    app/utils/ingest.py). For that equality rel_path is relative to
    LIBRARY_ROOT even when the run was pointed at a subfolder of it. That
    equality is what lets discover() recognise "already in the library" and
    what lets process() find a just-ingested item's own fingerprints back
    out of RecordingFingerprint by recording_id.
  * any other root is BROUGHT IN: rel_path is relative to the run root and
    each folder goes through _do_confirm's normal move/copy path per File
    Handling. Content dedup (find_duplicates) is the only duplicate check.

Several runs can exist at once (e.g. a one-folder Downloads run started while
a library run is mid-flight). ONE worker thread serves them all and, between
every item, picks the next from the running run with the fewest pending
items, so small runs jump ahead of a big backlog and the big run resumes
afterwards.
"""

import json
import os
import threading
import time
import traceback
import unicodedata
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import or_

from app.extensions import db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.recording import Recording, RecordingFingerprint
from app.models.quality import QualityAnalysis
from app.models.user import User
from app.utils.ingest import resolve_shows_in_dir, is_show_root
from app.utils.completeness import completeness_band
from app.utils.checksums import parse_checksum_file
from app.utils.format import format_partial_date
from app.utils.resolve import DEDUP_FP_TYPES
from app.utils.paths import is_within

# ONE worker thread per process serves every run (small-runs-first queue).
# _WORKER_THREAD is protected by _WORKER_LOCK and lets _start_worker refuse to
# start a second thread while one is alive (S3/S9): Pause then Resume within
# one item used to always start a fresh thread, so the old thread (still
# inside its slow _do_confirm call) and the new one both worked the same
# items at once.
_WORKER_LOCK = threading.Lock()
_WORKER_THREAD = None

# The worker's FFP/ST5 -> recording_id map, built once per worker lifetime
# (it picks one item at a time across runs, so rebuilding per item would
# re-parse every stored fingerprint for every folder). process() falls back to
# building its own when this is None, i.e. when called directly.
_dedup_cache = {"map": None}

# Run ids whose folder walk must be repeated even though the worker already
# did it once: a finished Review First run started again over the same folder
# (see start_run) may hold new folders. discover() is idempotent.
_REDISCOVER = set()

# Run ids reopened by recheck_queue: they already hold every item, so the worker need
# not walk the folder again before working them.
_SKIP_DISCOVER = set()

# A folder that is still downloading is skipped, left pending and not looked
# at again for this long, so the worker moves on to other items instead of
# spinning on it.
_DOWNLOAD_RETRY_SECS = 15
_DOWNLOADING = "downloading"
_CONVERTING = "converting"
# Reasons that mean "busy elsewhere, look again after the retry window".
_COOLING = (_DOWNLOADING, _CONVERTING)

# Set by _start_worker when the worker is already alive, so an idle worker
# backing off for _DOWNLOAD_RETRY_SECS wakes at once for new work.
_WAKE = threading.Event()

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


def _library_root():
    return os.path.realpath(str(current_app.config["LIBRARY_ROOT"]))


def is_in_library(root):
    """True when a run rooted at `root` catalogs in place: root is
    LIBRARY_ROOT or somewhere inside it."""
    return is_within(root, _library_root())


def item_base(root):
    """The directory BulkIngestItem.rel_path is relative to for a run rooted
    at `root`: LIBRARY_ROOT for an in-library run (so rel_path equals
    Recording.folder_path even for a library-subfolder run), else the run
    root itself."""
    return _library_root() if is_in_library(root) else os.path.realpath(root)


def item_abs_path(item):
    """The folder an item points at on disk. rel_path "." (an outside
    single-show source) is the run root itself."""
    return os.path.normpath(_on_disk_path(item_base(item.run.root), item.rel_path))


def _abs_key(base, rel):
    """NFC absolute path of an item -- the identity used across runs, since
    the same rel_path under two different bases is two different folders."""
    # normpath: an outside single-show run's item is rel "." -- without it
    # "Downloads/Show/." and a Downloads run's "Downloads/Show" would be two keys
    # for one folder.
    return unicodedata.normalize("NFC", os.path.normpath(os.path.join(base, rel)))


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


# Item statuses that make up a hold run's Queue once the worker has finished
# with it: a person still has something to do with them.
_QUEUE_WAITING = ("ready", "review")


def _has_waiting_queue(run):
    return (db.session.query(BulkIngestItem.id)
            .filter(BulkIngestItem.run_id == run.id,
                    BulkIngestItem.status.in_(_QUEUE_WAITING))
            .first()) is not None


def _active_run_for_root(root):
    """The unfinished run pointed at `root`, if any: running or paused, else a
    finished run (either mode) whose Queue still holds ready/review items (the
    queue persists, so starting the same folder again resumes it rather than
    creating a second queue for the same folders). A second start_run() for
    the same root returns it; a different root queues a new run."""
    run = (db.session.query(BulkIngestRun)
           .filter(BulkIngestRun.status.in_(("running", "paused")),
                   BulkIngestRun.root == root)
           .order_by(BulkIngestRun.id.desc())
           .first())
    if run:
        return run
    for run in (db.session.query(BulkIngestRun)
                .filter(BulkIngestRun.status == "done", BulkIngestRun.root == root)
                .order_by(BulkIngestRun.id.desc()).all()):
        if _has_waiting_queue(run):
            return run
    return None


def active_runs():
    """Every unfinished run, oldest first."""
    return (db.session.query(BulkIngestRun)
            .filter(BulkIngestRun.status.in_(("running", "paused")))
            .order_by(BulkIngestRun.id.asc())
            .all())


def listed_runs():
    """Runs the UI should list: every unfinished run, plus finished runs of
    either mode whose Queue still has ready/review items (unfinished from the
    person's point of view, so review work is never orphaned). Oldest first."""
    out = list(active_runs())
    seen = {r.id for r in out}
    for run in (db.session.query(BulkIngestRun)
                .filter(BulkIngestRun.status == "done")
                .all()):
        if run.id not in seen and _has_waiting_queue(run):
            out.append(run)
    return sorted(out, key=lambda r: r.id)


def applied_values(run):
    """The run's staged blanket values as a dict ({} when none or unreadable)."""
    if not run.applied_json:
        return {}
    try:
        v = json.loads(run.applied_json)
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _ingested_paths_all_runs():
    """
    The absolute path of every item any earlier run already turned into a
    Recording, across ALL runs -- not just the most recent one. Re-run's
    discover() uses this so a folder ingested two runs ago is never re-offered
    even though its own run finished long since (spec 5b "Re-run").

    Keyed by absolute path, not rel_path: with outside sources a rel_path is
    only meaningful against its own run's base, and "Show1" under Downloads is
    not the "Show1" already in the library.
    """
    rows = (db.session.query(BulkIngestItem.rel_path, BulkIngestRun.root)
            .join(BulkIngestRun, BulkIngestRun.id == BulkIngestItem.run_id)
            .filter(BulkIngestItem.status == "ingested")
            .all())
    bases = {}
    out = set()
    for rel, root in rows:
        if root not in bases:
            bases[root] = item_base(root)
        out.add(_abs_key(bases[root], _norm_rel(rel)))
    return out


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
    in_library = is_in_library(root)
    base = item_base(root)
    # For an in-library run pointed at a subfolder, rel_path still has to be
    # relative to LIBRARY_ROOT (== Recording.folder_path): prefix every
    # root-relative path with where the root sits under the base.
    prefix = os.path.relpath(os.path.realpath(root), base) if in_library else "."

    # A 'review' item (this run's or an earlier one's) may have been
    # accepted through Review & Ingest since it was written -- reconcile it
    # to 'ingested' BEFORE building already_ingested, so that folder's now-
    # existing Recording is recognised below instead of getting a second,
    # duplicate item ('skipped/already_in_library') (S4b). Every run, not
    # just this one: the folder could have been left in 'review' by a run
    # that finished long ago.
    _reconcile_review_items(run=None)

    already_ingested = _ingested_paths_all_runs()
    # Only an in-library source can already BE a Recording by location; an
    # outside source is judged by content (find_duplicates) at process time.
    existing_folder_paths = set() if not in_library else {
        _norm_rel(fp) for (fp,) in
        db.session.query(Recording.folder_path).filter(Recording.folder_path.isnot(None)).all()
    }

    def _rel_of(abs_path):
        r = os.path.relpath(abs_path, root)
        if r == ".":
            # The root is itself the show (single-folder run). In-library it
            # is its own path under LIBRARY_ROOT, so it equals folder_path;
            # outside it stays "." against the run root, which keeps the item
            # path and the absolute dedup key stable after the folder moves.
            return _norm_rel(prefix)
        return _norm_rel(r if prefix == "." else os.path.join(prefix, r))
    existing_items = {
        r for (r,) in
        db.session.query(BulkIngestItem.rel_path).filter(BulkIngestItem.run_id == run.id).all()
    }

    unreadable_paths = []
    # The library root is a container by definition, never a show (and a
    # library with folders named like discs would otherwise read as one).
    if os.path.realpath(root) != _library_root() and is_show_root(root):
        # One folder, one run: the root is the show, nothing to walk.
        show_paths = [root]
    else:
        show_paths = resolve_shows_in_dir(root, unreadable=unreadable_paths)

    pending_writes = 0
    for abs_path in show_paths:
        rel = _rel_of(abs_path)
        if rel in existing_items:
            continue
        if _abs_key(base, rel) in already_ingested:
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
        rel = _rel_of(abs_path)
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
    # `base` is what rel_path is relative to (LIBRARY_ROOT for an in-library
    # run, the run root otherwise); `in_library` decides in-place vs bring-in.
    in_library = is_in_library(run.root)
    base = item_base(run.root)
    user_id = _owner_user_id()
    hash_to_recording = (_dedup_cache["map"] if _dedup_cache["map"] is not None
                         else _build_dedup_map())

    while True:
        if stop_flag():
            return
        # A pending item flagged 'downloading' is passed over until the retry
        # window lapses, so a still-downloading folder cannot starve (or
        # spin) the rest of the queue.
        # Items a person asked to ingest (Review First's Ingest / Ingest all
        # ready) come first: they are explicit requests, and there are only as
        # many as the person pressed.
        requested = _requested_query(run.id).order_by(BulkIngestItem.id.asc()).first()
        item = requested
        if item is None:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=_DOWNLOAD_RETRY_SECS)
            item = (db.session.query(BulkIngestItem)
                    .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "pending",
                            or_(BulkIngestItem.reason.is_(None),
                                BulkIngestItem.reason.notin_(_COOLING),
                                BulkIngestItem.updated_at < cutoff))
                    .order_by(BulkIngestItem.id.asc())
                    .first())
        if item is None:
            break

        # R2-2: item.rel_path is deliberately NFC (the dedup key), but the
        # bytes actually on disk can be NFD -- resolve the real path to
        # scan/open rather than the one that merely LOOKS right.
        # normpath: an outside single-show item is rel "." and "<root>/." has
        # basename "." -- the resolver would lose the folder name (its date,
        # artist, source) and the staging row would be keyed apart from the
        # real path the per-item Review and promote_to_recording use.
        folder_abs = os.path.normpath(_on_disk_path(base, item.rel_path))

        # A bring-in item still being written by the download queue must not
        # be moved (the worker would recreate the folder and split the show).
        # The check and the move share FS_LOCK, as quality.py's Move does, so
        # a download cannot start between them. Only a Downloads folder can
        # be busy, so the lock is not taken for anything else.
        check_downloading = (not in_library and _in_downloads(folder_abs)
                             and requested is None)
        # A folder being converted (SHN/WAV to FLAC) is skipped the same way,
        # for a requested item too (its endpoint already refused one converting
        # at request time). Only an item that may be moved (bring-in, and
        # either an auto run or a person's request) takes FS_LOCK, for the
        # check and the ingest together, as convert_folder_start registers its
        # job under it. Review First's analyze/score path never moves files,
        # so it must not hold the lock through a long decode.
        from app.utils import download_queue
        from app.api.quality import converting_here
        will_move = not in_library and (run.mode != "hold" or requested is not None)
        with (download_queue.FS_LOCK if will_move else nullcontext()):
            busy = None
            if check_downloading and download_queue.downloading_here(folder_abs):
                busy = _DOWNLOADING
            elif requested is None and converting_here(folder_abs):
                busy = _CONVERTING
            if busy:
                item.reason = busy
                item.updated_at = datetime.now(timezone.utc)
                db.session.commit()
                continue
            _process_item(run, item, folder_abs, base, in_library, user_id,
                          hash_to_recording)

    # No pending items left -- the run is done, unless it was paused out from
    # under us between the last item and this check.
    _finish_if_drained(run)


def _requested_query(run_id):
    """Ready/review items a person asked to ingest and the worker has not yet
    done."""
    return (db.session.query(BulkIngestItem)
            .filter(BulkIngestItem.run_id == run_id,
                    BulkIngestItem.ingest_requested.is_(True),
                    BulkIngestItem.status.in_(_QUEUE_WAITING)))


def _in_downloads(folder_abs):
    """True when the folder sits directly inside the Downloads folder (the
    only place downloading_here can say yes)."""
    from app.utils.downloads_dir import downloads_dir
    parent = os.path.dirname(os.path.realpath(folder_abs.rstrip(os.sep)))
    return parent == os.path.realpath(downloads_dir())


def _process_item(run, item, folder_abs, base, in_library, user_id, hash_to_recording):
    """
    Work one item: gate -> resolve -> dedup -> verdict, then ingest it (auto
    run, or a person's request) or park it as 'ready' (hold run).

    A requested item (ingest_requested) always ingests. A flagged one is
    forced past the verdict ("Ingest anyway"); a ready one is not, so a verdict
    that has turned bad since analysis (e.g. a duplicate ingested meanwhile)
    sends it back to review instead of importing it.
    """
    from app.api.ingest import auto_confirm, MoveFailed

    library_root = base
    requested = bool(item.ingest_requested)
    force = requested and item.status in ("review", "pending")
    hold = run.mode == "hold" and not requested
    applied = applied_values(run)
    item.status = "in_progress"
    db.session.commit()

    try:
        # A folder that became a Recording under a DIFFERENT rel_path
        # between discover() and now (e.g. a resumed item whose folder
        # was ingested some other way in the meantime) is simply not
        # re-ingested. Location only means anything for an in-library
        # source; an outside one is judged by content in auto_confirm.
        already = (in_library and
                   db.session.query(Recording.id)
                   .filter(Recording.folder_path == item.rel_path)
                   .first())
        if already:
            item.status = "skipped"
            item.reason = "already_in_library"
            item.ingest_requested = False
            db.session.commit()
            return

        # The one server function that turns a folder into an ingested
        # (or reviewed, or skipped) recording, via the resolver -- Batch
        # Import's auto-confirm endpoint calls the exact same function
        # (spec section 4), so a bulk-ingested and an auto-ingested-from-
        # Batch-Import recording can never come out different.
        outcome = auto_confirm(folder_abs, user_id,
                               hash_cache=hash_to_recording,
                               hold=hold, force=force, applied=applied)
        status   = outcome["status"]
        reasons  = outcome["reasons"]
        resolved = outcome["resolved"]
        item.format = outcome.get("format")

        # Review First: live folders are scored now, before anything enters
        # the library, so the person sees the number while deciding. Ingest
        # promotes this staging row (promote_to_recording), and the audio
        # pass then skips scoring. Albums are never scored.
        # An in-place (library) run never scores here: scanning a whole
        # library's audio before anything is catalogued would take hours, and
        # the background audio pass scores it after ingest anyway.
        quality = None
        if (status == "ready" and resolved is not None and resolved.kind != "studio"
                and not in_library):
            quality = _score_before_ingest(folder_abs, base)

        # meta_band (2026-09-27 unified ingest queue table, 2026-10-05 completeness): the
        # High/Medium/Low band of how complete the resolver's reading is (app/utils/
        # completeness.py), computed here at extraction time and carried inside the existing
        # `meta` JSON blob (no schema change). It no longer turns Low for a row in review.
        meta_band = completeness_band(resolved) if resolved is not None else None   # unread: no band

        date = (resolved.date.value if resolved else None) or {}
        track_count, track_titles = _scan_tracks(resolved.scan if resolved else None)
        # 2026-09-27 progress/log redesign: the log's expand panel needs
        # these fields without a per-item re-scan -- stash whatever the
        # resolver found, win or lose (a review/failed item still has
        # partial data worth showing).
        item.meta = json.dumps({
            "artist":    resolved.artist.value if resolved else None,
            "date_text": format_partial_date(date.get("year"), date.get("month"),
                                             date.get("day")),
            "venue":     resolved.venue.value if resolved else None,
            "event":     resolved.event.value if resolved else None,
            "city":      resolved.city.value if resolved else None,
            "state":     resolved.state.value if resolved else None,
            "country":   resolved.country.value if resolved else None,
            "source":    resolved.source.value if resolved else None,
            "lineage":   resolved.lineage.value if resolved else None,
            "title":     resolved.album.value if resolved else None,
            "meta_band": meta_band,
            "meta_band_v": 2,        # completeness band (schema_upgrades.ensure_bulk_meta_band skips these)
            "listening_quality": quality,
            "track_count":  track_count,
            "tracks":       track_titles,
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
        elif status == "ready":
            item.status = "ready"
            item.reason = None
            item.kind = resolved.kind if resolved else None
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
        item.ingest_requested = False
        db.session.commit()
    except MoveFailed as e:
        # move_to_library failed partway (a bring-in item; part of the show
        # may already be in the library). Retrying blindly would re-ingest
        # whatever is left in the source, so a person looks at it. The run
        # carries on with its other items.
        db.session.rollback()
        item = db.session.get(BulkIngestItem, item.id)
        item.status = "review"
        item.reason = "move_failed"
        item.detail = str(e)
        item.ingest_requested = False
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
        item.ingest_requested = False
        db.session.commit()
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        item = db.session.get(BulkIngestItem, item.id)
        item.status = "failed"
        item.reason = None
        item.detail = str(e)
        item.ingest_requested = False
        db.session.commit()
        traceback.print_exc()


def _scan_tracks(scan):
    """(count, [{n, title}]) for the log's expand panel. Titles come from the
    info file when it lists tracks, else from the files' own tags; both are
    what the review page would show. Capped so the meta blob stays small."""
    if not scan:
        return None, []
    sug = scan.get("suggestions") or {}
    info = (sug.get("from_info_file") or {}).get("tracks") or []
    tags = (sug.get("from_tags") or {}).get("tracks") or []
    rows = ([{"n": t.get("number"), "title": t.get("title")} for t in info]
            or [{"n": t.get("track_number"), "title": t.get("title")} for t in tags])
    rows = [r for r in rows if r["title"]][:200]
    return scan.get("audio_file_count"), rows


def _score_before_ingest(folder_abs, base):
    """
    Listening Quality for a folder that is not in the library yet, through the
    same scorer and staging cache Review & Ingest uses: a current analysis for
    the folder is reused, never recomputed. Returns the 0-100 score or None
    (unreadable audio records its error on the staging row and the audio pass
    scores the recording after ingest instead).
    """
    from app.api.quality import _analyse_one, _is_current
    from app.utils import quality_store as qs

    if not _is_current(folder_abs):
        _analyse_one(folder_abs, qs.norm_path(base))
    row = qs.get_staging(folder_abs)
    return row.listening_quality if row is not None else None


def _finish_if_drained(run):
    """Mark `run` done once nothing is pending, unless it was paused out from
    under us between the last item and this check. Items still waiting on a
    download count as pending, so such a run stays open."""
    run = db.session.get(BulkIngestRun, run.id)
    if run.status != "running":
        return
    remaining = (db.session.query(BulkIngestItem.id)
                .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "pending")
                .first())
    if remaining or _requested_query(run.id).first():
        return
    run.status = "done"
    run.finished_at = datetime.now(timezone.utc)
    # A hold run's ready items are NOT outstanding work for the worker: they
    # wait for a person (see listed_runs for how they stay visible).
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
         .filter(BulkIngestItem.status.in_(_QUEUE_WAITING)))
    if run is not None:
        q = q.filter(BulkIngestItem.run_id == run.id)

    reconciled = 0
    for item, item_run in q.all():
        folder_path = norm_path(os.path.normpath(
            os.path.join(item_base(item_run.root), item.rel_path)))
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


# ── Queue re-check after a person confirms an act, venue or event (chunk 8) ──
# A review row waits on a reading ("Sam Bush Band", "Wilkes Commnity College").
# Once a person has settled that reading on one row, every other waiting row that
# quotes the same text should not ask again. Re-checking is just re-processing:
# the row goes back to pending and the worker reads the folder again, now against
# a library that holds the confirmed row and its learned alias. The worker then
# does what the run's mode says (Import Automatically ingests a row that reaches
# the verdict, Review First parks it Ready), and the run's staged blanket values
# still overwrite whatever it reads, so human-set values stay locked.

# Reasons no confirmation of an act, venue or event can change.
_NOT_RECHECKABLE = ("unsupported_format", "move_failed", "duplicate_content", "unreadable",
                    "no_audio", _DOWNLOADING, _CONVERTING)


def _meta_dict(item):
    try:
        v = json.loads(item.meta) if item.meta else {}
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def recheck_queue(folder_path, keys):
    """After a person's save of `folder_path`: put the other review rows of the same run that
    share a confirmed reading back to pending. `keys` is {"artist"|"venue"|"event": {norm keys}}
    (app.utils.aliases.confirmed_keys). A folder that is not in a listed run changes nothing.
    Returns the item ids sent back."""
    from app.utils.reader.library import norm_key
    if not folder_path or not keys:
        return []
    want = os.path.realpath(str(folder_path))
    run, own = None, None
    for r in listed_runs():
        rel = os.path.relpath(want, item_base(r.root))
        if rel == ".." or rel.startswith(".." + os.sep):
            continue
        rp = _norm_rel(rel) if rel != "." else "."
        own = (db.session.query(BulkIngestItem)
               .filter(BulkIngestItem.run_id == r.id,
                       BulkIngestItem.rel_path == rp,
                       BulkIngestItem.status.in_(_QUEUE_WAITING + ("in_progress",)))
               .first())
        if own:
            run = r
            break
    if run is None:
        return []
    sent = []
    for it in (db.session.query(BulkIngestItem)
               .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "review",
                       BulkIngestItem.ingest_requested.is_(False),
                       BulkIngestItem.id != own.id)):
        reasons = (it.reason or "").split(",")
        if set(reasons) & set(_NOT_RECHECKABLE):
            continue
        if not any(f in r for r in reasons for f in ("artist", "venue", "event")):
            continue
        meta = _meta_dict(it)
        if any(meta.get(f) and norm_key(meta[f]) in ks for f, ks in keys.items()):
            it.status = "pending"
            it.reason = None
            it.detail = None
            sent.append(it.id)
    if sent:
        if run.status == "done":
            run.status = "running"
            run.finished_at = None
            _SKIP_DISCOVER.add(run.id)
        db.session.commit()
        _start_worker()
    return sent


def lead_first(rows):
    """Order review rows so one row per unconfirmed act comes first, then the rest in their
    existing (id) order. `rows` is [(id, meta json)] in id order; returns the ids. An act is
    unconfirmed while the library cannot resolve its reading (an Artist row, a learned alias, or the act core); rows with no reading are not leads.
    Stable and one pass: the first row of each reading leads it."""
    from app.utils.reader.library import current_library, norm_key
    ix = current_library()
    seen, lead, rest = set(), [], []
    for rid, meta in rows:
        try:
            a = (json.loads(meta) if meta else {}).get("artist")
        except ValueError:
            a = None
        k = norm_key(a) if isinstance(a, str) else ""
        if k and k not in seen and ix.artist_match(a) is None:
            seen.add(k)
            lead.append(rid)
        else:
            rest.append(rid)
    return lead + rest


def _pick_run(discovered):
    """
    The running run the worker should serve next, or None.

    Small runs first: among running runs that still have workable pending
    items, the one with the FEWEST pending items wins (ties: oldest). A run
    not yet discovered is discovered here first -- cheap for a single outside
    folder, a real walk for a big library. A running run with nothing pending
    at all is returned at once so process() can mark it done. Runs whose only
    pending items are cooling down after a 'downloading' skip are passed over
    (they come back after _DOWNLOAD_RETRY_SECS).
    """
    runs = (db.session.query(BulkIngestRun)
            .filter(BulkIngestRun.status == "running")
            .order_by(BulkIngestRun.id.asc())
            .all())
    for r in runs:
        if r.id in _SKIP_DISCOVER:
            _SKIP_DISCOVER.discard(r.id)
            if r.id not in _REDISCOVER:
                discovered.add(r.id)
        if r.id not in discovered or r.id in _REDISCOVER:
            discover(r)
            discovered.add(r.id)
            _REDISCOVER.discard(r.id)

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_DOWNLOAD_RETRY_SECS)
    best, best_n = None, None
    for r in runs:
        if _requested_query(r.id).first():
            return r
        pending = (db.session.query(BulkIngestItem.id)
                   .filter(BulkIngestItem.run_id == r.id, BulkIngestItem.status == "pending")
                   .count())
        if pending == 0:
            return r
        workable = (db.session.query(BulkIngestItem.id)
                    .filter(BulkIngestItem.run_id == r.id, BulkIngestItem.status == "pending",
                            or_(BulkIngestItem.reason.is_(None),
                                BulkIngestItem.reason.notin_(_COOLING),
                                BulkIngestItem.updated_at < cutoff))
                    .count())
        if workable and (best is None or pending < best_n):
            best, best_n = r, pending
    return best


def _pending_anywhere():
    """True when some running run still has pending items (e.g. ones waiting
    on a download) or a person's ingest request, so the worker must stay alive
    for them."""
    return (db.session.query(BulkIngestItem.id)
            .join(BulkIngestRun, BulkIngestRun.id == BulkIngestItem.run_id)
            .filter(BulkIngestRun.status == "running",
                    or_(BulkIngestItem.status == "pending",
                        (BulkIngestItem.ingest_requested.is_(True)
                         & BulkIngestItem.status.in_(_QUEUE_WAITING))))
            .first()) is not None


def _one_item_flag(run_id):
    """stop_flag for process(): False for the first poll (unless the run
    was paused), True afterwards -- so process() does exactly one item and
    the worker re-picks, letting a smaller run jump the queue."""
    calls = {"n": 0}

    def flag():
        calls["n"] += 1
        if calls["n"] > 1:
            return True
        r = db.session.get(BulkIngestRun, run_id)
        return r is None or r.status != "running"
    return flag


def _start_worker(run_id=None, app=None):
    """Start THE worker thread unless one is already alive. run_id is accepted
    for the callers that still pass it; the worker serves every running run."""
    global _WORKER_THREAD
    if app is None:
        app = current_app._get_current_object()

    def _leaving():
        """
        R2-3: called when no run is workable. The thread is still
        `is_alive()` for as long as it takes to actually return, and a Resume
        landing in that window used to see an "alive" worker and refuse to
        start a new one, even though this one had already committed to
        leaving.

        Re-checks for running work and clears _WORKER_THREAD under the SAME
        lock _start_worker's own is_alive() check uses, so a concurrent
        Resume/start either commits before this runs (work is seen, the loop
        continues) or after the clear (nothing looks alive, a fresh worker
        starts). Returns True when the worker should actually stop.
        """
        global _WORKER_THREAD
        with _WORKER_LOCK:
            if _pending_anywhere():
                return False
            _WORKER_THREAD = None
            return True

    def _pause_with_error(run_id):
        # R2-N5: leave the run 'paused', not 'running', so the page offers
        # Resume instead of showing a run nobody is driving and polling
        # forever with no worker to catch up to. Only the run that failed;
        # the other runs keep going.
        db.session.rollback()
        r = db.session.get(BulkIngestRun, run_id)
        if r is not None:
            r.last_error = traceback.format_exc()
            r.status = "paused"
            db.session.commit()

    def _work():
        global _WORKER_THREAD
        with app.app_context():
            discovered = set()
            try:
                _dedup_cache["map"] = _build_dedup_map()
                while True:
                    _WAKE.clear()
                    try:
                        r = _pick_run(discovered)
                    except Exception:  # noqa: BLE001
                        # discover() of one run failed; pause the first run
                        # that is not yet discovered (the one it was walking).
                        traceback.print_exc()
                        bad = (db.session.query(BulkIngestRun)
                               .filter(BulkIngestRun.status == "running",
                                       ~BulkIngestRun.id.in_(discovered or {0}))
                               .order_by(BulkIngestRun.id.asc()).first())
                        if bad is None:
                            raise
                        _pause_with_error(bad.id)
                        continue
                    if r is None:
                        if _leaving():
                            return
                        # Only downloading/converting folders are left: back off
                        # for the retry window (they are skipped until then
                        # anyway), but wake early for new work.
                        _WAKE.wait(_DOWNLOAD_RETRY_SECS)
                        continue
                    run_id = r.id
                    try:
                        process(r, _one_item_flag(run_id))
                    except Exception:  # noqa: BLE001
                        traceback.print_exc()
                        _pause_with_error(run_id)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                with _WORKER_LOCK:
                    _WORKER_THREAD = None
                # Nobody is left to drive the running runs: pause them all.
                for r in (db.session.query(BulkIngestRun)
                          .filter(BulkIngestRun.status == "running").all()):
                    _pause_with_error(r.id)
            finally:
                # Not if a fresh worker has already started and built its own.
                with _WORKER_LOCK:
                    if _WORKER_THREAD in (None, threading.current_thread()):
                        _dedup_cache["map"] = None

    with _WORKER_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            # The worker already serves every running run (e.g. Resume called
            # while it is still mid-item) -- never start a second one.
            _WAKE.set()
            return
        t = threading.Thread(target=_work, daemon=True)
        _WORKER_THREAD = t
        t.start()


def default_mode(root):
    """Import Automatically for the library folder, Review First for
    anywhere else (Downloads and other folders)."""
    return "auto" if is_in_library(root) else "hold"


def start_run(root, mode=None):
    """
    Return the unfinished run already pointed at `root` (keeping ITS mode),
    else create a new one (status running) and make sure the worker is going.
    A run for a different root while another is active simply queues behind
    the worker's small-runs-first picking; nothing is refused or replaced.
    `mode` is 'auto' or 'hold'; None picks default_mode(root).
    """
    run = _active_run_for_root(root)
    if run:
        if run.status == "done":
            # A Review First queue being picked up again: look for new
            # folders and let the worker take it from there.
            run.status = "running"
            run.finished_at = None
            db.session.commit()
            _REDISCOVER.add(run.id)
            _start_worker()
        return run
    run = BulkIngestRun(root=root, status="running", mode=mode or default_mode(root))
    db.session.add(run)
    db.session.commit()
    _start_worker()
    return run


def pause_run(run):
    run.status = "paused"
    db.session.commit()


def resume_run(run):
    run.status = "running"
    db.session.commit()
    _start_worker()


def reset_queue(run):
    """
    Empty a run's Queue and close the run. Removes every item that did not
    become a recording (and was not sent to Backlog/Workshop); files on disk
    are never touched. Idempotent.

    The run is marked done first, so the worker stops picking it between
    items. An item that is 'in_progress' right now is mid-ingest inside the
    worker: deleting its row would break that commit, so it is left to finish
    (it ends as ingested or flagged, like any other item). Returns the number
    of rows removed.
    """
    if run.status != "done":
        run.status = "done"
        run.finished_at = datetime.now(timezone.utc)
    removed = (db.session.query(BulkIngestItem)
               .filter(BulkIngestItem.run_id == run.id,
                       BulkIngestItem.status.notin_(("ingested", "moved", "in_progress")))
               .delete(synchronize_session=False))
    _REDISCOVER.discard(run.id)
    db.session.commit()
    return removed


def reset_in_progress(run):
    """
    Any item still 'in_progress' on `run` was mid-bulk_ingest at whatever moment
    stopped the worker (an app restart, a simulated crash in a test) and its
    outcome is unknown -- _do_confirm's move is not safely resumable
    partway through, so it goes back to 'pending' to be tried again from
    scratch. Split out from resume_on_boot() so a test can exercise exactly
    this reset without needing a live app/thread around it.
    """
    q = (db.session.query(BulkIngestItem)
         .filter(BulkIngestItem.run_id == run.id, BulkIngestItem.status == "in_progress"))
    if is_in_library(run.root):
        # In place: nothing was moved, so trying again is safe.
        q.update({"status": "pending"}, synchronize_session=False)
    else:
        # Bring-in: move_to_library moves file by file, so the source may be
        # half emptied. A person looks before anything is re-ingested.
        q.update({"status": "review", "reason": "interrupted"}, synchronize_session=False)
    db.session.commit()


def resume_on_boot(app):
    """
    Called from create_app (never in SERVER_MODE): if a run was left
    'running' when the process last stopped, reset its in-progress item (see
    reset_in_progress) and start the worker. Every unfinished running run is
    resumed. A run that was 'paused' is left exactly as it was; only the
    caller pressing Resume puts it back in the queue.

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
            runs = (db.session.query(BulkIngestRun)
                    .filter(BulkIngestRun.status == "running")
                    .order_by(BulkIngestRun.id.asc())
                    .all())
        except Exception:  # noqa: BLE001
            db.session.rollback()
            return
        # Every unfinished run resumes; the single worker sorts out the order.
        for run in runs:
            reset_in_progress(run)
        if runs:
            _start_worker(app=app)

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
