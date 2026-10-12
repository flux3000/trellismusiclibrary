"""utils/debug_log.py — shared in-memory debug log buffer.

Was previously a module-level deque inside app/api/debug.py, holding only
entries forwarded from the JS fetch wrapper (one line per API call, AFTER it
completes). Moved here and extended with log_step() so server-side code —
scan_folder(), read_flac_tags(), parse_info_file(), Paula scoring, the bulk
ingest copy loop — can push checkpoints DURING a long-running operation, not
just report a single before/after timing line once it's over. That's the gap
that made a stuck Batch Import "Review" scan invisible: the debug panel had
no way to show anything about a request that hadn't finished yet.

Requires `threaded=True` on the Flask dev server (see run.py) to be useful —
a single-threaded server blocked on the slow request being investigated
can't also answer a request for /api/debug/live to show these checkpoints.

Process-scoped, not strictly thread-safe, but deque.append/list() are
effectively atomic under the GIL — fine for single-dev DEV_MODE use.
"""

import time
from collections import deque

_dbg_log = deque(maxlen=400)


def _dev_mode_on():
    try:
        from flask import current_app
        return bool(current_app.config.get("DEV_MODE"))
    except RuntimeError:
        return False   # no active app/request context (e.g. a bare background thread)


def log_entry(entry):
    """Append a raw dict entry as-is — used by POST /api/debug/log (JS-forwarded)."""
    _dbg_log.appendleft(entry)


def log_step(job, stage, detail=None, **extra):
    """
    Log ONE checkpoint from inside a server-side pipeline.

    `job` groups related steps together (e.g. a scan's folder path, or an
    ingest job id) so the panel can show progress for THIS operation
    specifically. Silently no-ops outside DEV_MODE so call sites never need
    to guard themselves, and no-ops without an app/request context so a
    background thread with no active request doesn't need special-casing
    either (pass the flag explicitly via `force=True` if logging from one).
    """
    if not (extra.pop("force", False) or _dev_mode_on()):
        return
    _dbg_log.appendleft({
        "kind":   "step",
        "job":    job,
        "stage":  stage,
        "detail": detail,
        "ts":     time.time(),
        **extra,
    })


def all_entries():
    return list(_dbg_log)


def clear():
    _dbg_log.clear()
