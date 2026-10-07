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
    move_to_library(). Ingested recordings are unaffected either way: they keep
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


# ── File handling (2026-09-25) ───────────────────────────────────────────────
#
# One install-level mode, `file_handling_mode` (`keep` / `organize`), that
# governs six independently-editable switches. Absent reads as `keep`; the
# migration seeds `organize` on any database that already has recording rows
# (an existing library already looks organized; a fresh one has not chosen
# yet). Placement (`file_under_artist_folder`, above) is a separate axis and
# is exposed here under the name `placement` for convenience, not stored
# under a new key.

FILE_HANDLING_MODE_KEY   = "file_handling_mode"
RENAME_FOLDERS_KEY       = "rename_folders"
RENAME_FILES_KEY         = "rename_files"
NAMING_SCHEME_KEY        = "naming_scheme"
NAMING_TEMPLATE_KEY      = "naming_template"
WRITE_TAGS_ON_INGEST_KEY = "write_tags_on_ingest"
WRITE_TAGS_DEFAULT_KEY   = "write_tags_default"

VALID_MODES   = ("keep", "organize")
VALID_SCHEMES = ("original", "number_title", "etree_tracks", "etree", "etree_sets", "custom")

# The six switches a mode change rewrites. Bool-valued keys are stored as the
# literal strings "true"/"false"; naming_scheme is stored as-is.
_BOOL_KEYS = (RENAME_FOLDERS_KEY, RENAME_FILES_KEY,
              WRITE_TAGS_ON_INGEST_KEY, WRITE_TAGS_DEFAULT_KEY)

MODE_DEFAULTS = {
    "keep": {
        RENAME_FOLDERS_KEY:       False,
        RENAME_FILES_KEY:         False,
        NAMING_SCHEME_KEY:        "original",
        WRITE_TAGS_ON_INGEST_KEY: False,
        WRITE_TAGS_DEFAULT_KEY:   False,
    },
    "organize": {
        RENAME_FOLDERS_KEY:       True,
        RENAME_FILES_KEY:         True,
        NAMING_SCHEME_KEY:        "number_title",
        WRITE_TAGS_ON_INGEST_KEY: True,
        WRITE_TAGS_DEFAULT_KEY:   True,
    },
}


def _get_setting(key):
    row = db.session.get(NodeSetting, key)
    return row.value if row else None


def _set_setting(key, value):
    row = db.session.get(NodeSetting, key)
    if row:
        row.value = value
    else:
        db.session.add(NodeSetting(key=key, value=value))


def get_file_handling():
    """The full file-handling state, defaults applied. Every switch not yet
    written falls back to its mode's default, so a fresh install and a
    partially-configured one both read as a complete, valid dict."""
    mode = _get_setting(FILE_HANDLING_MODE_KEY)
    if mode not in VALID_MODES:
        mode = "keep"
    defaults = MODE_DEFAULTS[mode]

    result = {FILE_HANDLING_MODE_KEY: mode}
    for key in _BOOL_KEYS:
        raw = _get_setting(key)
        result[key] = (raw == "true") if raw is not None else defaults[key]

    scheme = _get_setting(NAMING_SCHEME_KEY)
    result[NAMING_SCHEME_KEY] = scheme if scheme in VALID_SCHEMES else defaults[NAMING_SCHEME_KEY]
    result[NAMING_TEMPLATE_KEY] = _get_setting(NAMING_TEMPLATE_KEY) or ""
    result["placement"] = "artist" if file_under_artist_folder() else "root"
    return result


def apply_mode(mode):
    """Set file_handling_mode and rewrite the six switches to that mode's
    defaults. The switches remain individually editable afterwards."""
    if mode not in VALID_MODES:
        raise ValueError(f"file_handling_mode must be one of {VALID_MODES}")
    _set_setting(FILE_HANDLING_MODE_KEY, mode)
    for key, value in MODE_DEFAULTS[mode].items():
        _set_setting(key, ("true" if value else "false") if key in _BOOL_KEYS else value)
    db.session.commit()
    return get_file_handling()


def set_file_handling(**changes):
    """Validate and persist any subset of the individual switches, plus
    `placement`. Does not touch file_handling_mode -- that goes through
    apply_mode() because changing the mode rewrites the other switches too,
    which a plain field-by-field write must not do."""
    allowed = {RENAME_FOLDERS_KEY, RENAME_FILES_KEY, NAMING_SCHEME_KEY,
               NAMING_TEMPLATE_KEY, WRITE_TAGS_ON_INGEST_KEY,
               WRITE_TAGS_DEFAULT_KEY, "placement"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unknown file handling setting(s): {sorted(unknown)}")

    for key in _BOOL_KEYS:
        if key in changes and not isinstance(changes[key], bool):
            raise ValueError(f"{key} must be true or false")
    if NAMING_SCHEME_KEY in changes and changes[NAMING_SCHEME_KEY] not in VALID_SCHEMES:
        raise ValueError(f"naming_scheme must be one of {VALID_SCHEMES}")
    if "placement" in changes and changes["placement"] not in ("artist", "root"):
        raise ValueError("placement must be 'artist' or 'root'")

    # A custom template is parsed before it is ever written, so a broken one
    # (unknown token, unbalanced bracket, a path separator) never reaches the
    # column a save later reads as the active scheme's template (spec section
    # 4/12 -- "an invalid template cannot be saved"). Only static parse
    # validity is checked here; the empty-render case depends on a track's
    # own data and is already surfaced by the preview endpoint.
    effective_scheme = changes.get(NAMING_SCHEME_KEY) or _get_setting(NAMING_SCHEME_KEY)
    if effective_scheme == "custom":
        effective_template = changes.get(NAMING_TEMPLATE_KEY)
        if effective_template is None:
            effective_template = _get_setting(NAMING_TEMPLATE_KEY)
        # R4 (review, 2026-09-25): S9's "reject custom with an empty
        # template" HERE, at save time, made Custom unselectable on a fresh
        # install -- the Settings select saves naming_scheme='custom' alone
        # the moment it's picked, before the (until-then readonly) Template
        # field has anything in it, so that first save always hit this
        # rejection and reverted the select. naming_scheme=custom with an
        # empty template is now accepted as a transitional state: Settings
        # unlocks the Template field once the stored scheme reads 'custom',
        # the user types a template, and THAT save (naming_template alone,
        # scheme already 'custom') is what gets parse-validated below.
        # Ingest time still refuses an empty template outright --
        # rename_plan()/compute_audio_rename_map() raise their own
        # TemplateError before any file is touched, unchanged by this.
        if effective_template:
            from app.utils.file_naming import parse_template, TemplateError
            try:
                parse_template(effective_template)
            except TemplateError as e:
                raise ValueError(str(e))

    changes = dict(changes)
    if "placement" in changes:
        set_file_under_artist_folder(changes.pop("placement") == "artist")

    for key, value in changes.items():
        if key in _BOOL_KEYS:
            _set_setting(key, "true" if value else "false")
        else:
            _set_setting(key, value or "")
    db.session.commit()
    return get_file_handling()


# ── Download queue ───────────────────────────────────────────────────────────

DOWNLOAD_QUEUE_PAUSED_KEY = "download_queue_paused"


def download_queue_paused():
    """Paused means the worker takes no NEW job; a running one finishes. A node
    setting rather than a column: it is a fact about the queue, not a job.
    (2026-10-01)"""
    row = db.session.get(NodeSetting, DOWNLOAD_QUEUE_PAUSED_KEY)
    return bool(row and row.value == "true")


def set_download_queue_paused(paused):
    value = "true" if paused else "false"
    row = db.session.get(NodeSetting, DOWNLOAD_QUEUE_PAUSED_KEY)
    if row:
        row.value = value
    else:
        db.session.add(NodeSetting(key=DOWNLOAD_QUEUE_PAUSED_KEY, value=value))
    db.session.commit()
    return paused
