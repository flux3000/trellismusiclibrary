"""
app/utils/paths.py -- path containment checks shared by the API modules and
the Bulk Ingest worker.
"""

import os

from flask import current_app


def is_within(path, base):
    """True when realpath(path) is base itself or nested inside it. Both are
    realpath'd here so a symlink or NFD/NFC spelling cannot slip past."""
    p = os.path.realpath(path)
    b = os.path.realpath(base)
    return p == b or p.startswith(b + os.sep)


def within_import_roots(path):
    """True when `path` resolves to, or inside, one of the configured
    IMPORT_ROOTS -- the filesystem allowlist every browse/stream/ingest
    endpoint is gated on."""
    # The admin is the owner of this Mac: their music can live anywhere, so
    # the allowlist does not apply to them. It still binds every other role
    # (viewer/listener on a LAN or headless node).
    from flask import has_request_context
    if has_request_context():
        from flask_login import current_user
        if getattr(current_user, "is_authenticated", False) \
                and getattr(current_user, "role", None) == "admin":
            return True
    roots = current_app.config.get("IMPORT_ROOTS", [])
    return any(is_within(path, r) for r in roots)


# ── Where a recording's folder is ─────────────────────────────────────────────
# Recording.folder_path is relative to the root its `location` names: LIBRARY_ROOT
# for "library", the configured working folder for "workshop" / "backlog".

LOCATIONS = ("queue", "workshop", "backlog", "library")
WORKING_LOCATIONS = ("queue", "workshop", "backlog")      # everything that is not on the shelf


def location_root(location, library_root=None):
    """The absolute root a recording's folder_path is relative to, or "" when
    that root is not configured (and always "" for the queue, which has no root
    of its own). `library_root` lets a caller that was handed its own library
    root keep using it for library recordings."""
    loc = (location or "library")
    if loc == "library":
        return str(library_root or current_app.config.get("LIBRARY_ROOT") or "")
    if loc == "queue":
        return ""
    return str((current_app.config.get("TRIAGE_DIRS") or {}).get(loc) or "")


def queue_item_for(rec):
    """The BulkIngestItem a queue recording is linked to (newest first), or None."""
    from app.extensions import db
    from app.models.bulk_ingest import BulkIngestItem
    return (db.session.query(BulkIngestItem)
            .filter(BulkIngestItem.recording_id == rec.id)
            .order_by(BulkIngestItem.id.desc()).first())


def recording_folder_abs(rec, library_root=None):
    """Absolute folder of a recording: its location's root + folder_path. A queue
    recording resolves through its linked BulkIngestItem (run root + rel_path).
    Not realpath'd; callers that act on it check containment against
    location_root(rec.location) themselves. "" when there is no folder or the
    root is not configured."""
    if getattr(rec, "location", None) == "queue":
        it = queue_item_for(rec)
        if it is None:
            return ""
        from app.utils.bulk_ingest_run import item_abs_path
        return item_abs_path(it)
    root = location_root(getattr(rec, "location", None), library_root)
    rel = getattr(rec, "folder_path", None) or ""
    if not root or not rel:
        return ""
    return os.path.join(root, rel)
