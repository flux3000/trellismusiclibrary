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

# Whether new ingests are filed under an <Artist>/ directory (2026-09-17).
# An INSTALL-level setting, not a per-user preference: a library has one
# shape, and two users on one install disagreeing would produce exactly the
# hybrid tree this setting exists to prevent. Defaults ON, which is what
# every library Trellis laid out itself already looks like -- an existing
# install must not change shape because a new key appeared.
FILE_UNDER_ARTIST_KEY = "file_under_artist_folder"


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


# ── Library layout ───────────────────────────────────────────────────────────

def file_under_artist_folder():
    """
    True when a new ingest should land in LIBRARY_ROOT/<Artist>/<folder>.

    False files it flat at the library root. That is for the collector who
    pointed Trellis at a library they built themselves, where the artist is
    already in the folder name and every show sits at one level -- see
    move_to_library(). Adopted recordings are unaffected either way: they keep
    whatever path they were found at, and rename_recording_folder() renames
    the leaf under its existing parent rather than relocating anything.
    """
    row = db.session.get(NodeSetting, FILE_UNDER_ARTIST_KEY)
    return True if row is None else row.value == "true"


def set_file_under_artist_folder(enabled):
    """Persist the layout choice. Stored as the literal string "true"/"false"
    because NodeSetting.value is a string column and a bare bool would arrive
    back as the truthy "False"."""
    value = "true" if enabled else "false"
    row = db.session.get(NodeSetting, FILE_UNDER_ARTIST_KEY)
    if row:
        row.value = value
    else:
        db.session.add(NodeSetting(key=FILE_UNDER_ARTIST_KEY, value=value))
    db.session.commit()
    return enabled
