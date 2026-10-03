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
    # the allowlist does not apply to them (Ryan, 2026-10-02). It still
    # binds every other role (viewer/listener on a LAN or headless node).
    from flask import has_request_context
    if has_request_context():
        from flask_login import current_user
        if getattr(current_user, "is_authenticated", False) \
                and getattr(current_user, "role", None) == "admin":
            return True
    roots = current_app.config.get("IMPORT_ROOTS", [])
    return any(is_within(path, r) for r in roots)
