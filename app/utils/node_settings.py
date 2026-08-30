"""
app/utils/node_settings.py -- env-first, DB-fallback settings for this install.

SHARE_BASE_URL started as env-var-only (2026-08-25): fine for the headless
share node (run_headless.py takes real env vars), useless for the desktop
app that actually mints invites (a Dock launch gets none). Decided
2026-08-27: keep the env var as an override for the server-mode deployment,
but fall back to a value stored in the node_setting table so the desktop app
can set it from Settings instead of a shell.
"""

import os

from app.extensions import db
from app.models.node_setting import NodeSetting

SHARE_BASE_URL_KEY = "share_base_url"


def get_share_base_url():
    """Env var wins when set; otherwise whatever was saved from Settings."""
    env = os.environ.get("SHARE_BASE_URL")
    if env:
        return env
    row = db.session.get(NodeSetting, SHARE_BASE_URL_KEY)
    return row.value if row else None


def share_base_url_from_env():
    return bool(os.environ.get("SHARE_BASE_URL"))


def set_share_base_url(url):
    """Persists (or clears, if url is empty) the stored fallback. Does not
    touch the env var -- if one is set, it keeps winning until unset."""
    url = (url or "").strip().rstrip("/") or None
    row = db.session.get(NodeSetting, SHARE_BASE_URL_KEY)
    if url is None:
        if row:
            db.session.delete(row)
    elif row:
        row.value = url
    else:
        db.session.add(NodeSetting(key=SHARE_BASE_URL_KEY, value=url))
    db.session.commit()
    return url
