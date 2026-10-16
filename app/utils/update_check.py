"""app/utils/update_check.py -- check-only update notifier.

Fetches a small static manifest (UPDATE_MANIFEST_URL) at most once per 24h,
compares it to version.__version__, and remembers the result in node_setting.
It never downloads or installs anything: the app only tells the owner a newer
version exists and where to get it.

Every failure is logged and swallowed. Off under TESTING (like musicbrainz.py)
and never started in SERVER_MODE.
"""

import json
import logging
import sys
import threading
import time
import platform
import urllib.request

from flask import current_app

from app.extensions import db
from app.models.node_setting import NodeSetting
from app.utils.net import SSL_CONTEXT, USER_AGENT
from version import __version__

log = logging.getLogger(__name__)

DEFAULT_MANIFEST_URL = "https://trellismusiclibrary.com/releases/latest.json"
CHECK_INTERVAL_S = 24 * 60 * 60
FETCH_TIMEOUT_S = 10
MAX_BYTES = 64 * 1024
STARTUP_DELAY_S = 5
MAX_URL_LEN = 180  # node_setting.value is String(500); three fields must fit

ENABLED_KEY = "update_check_enabled"
LAST_CHECK_KEY = "update_last_check"
LAST_RESULT_KEY = "update_last_result"
SKIPPED_KEY = "update_skipped_version"


# ── version comparison ───────────────────────────────────────────────────────

def parse_version(text):
    """'v0.2.10' -> (0, 2, 10). None when it is not a plain dotted number.
    A pre-release suffix ('0.3.0-beta1') is ignored, so it compares equal to
    the release it precedes; Trellis does not publish those."""
    if not isinstance(text, str):
        return None
    core = text.strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    return a is not None and b is not None and a > b


# ── settings ─────────────────────────────────────────────────────────────────

def _get(key):
    row = db.session.get(NodeSetting, key)
    return row.value if row else None


def _set(key, value):
    row = db.session.get(NodeSetting, key)
    if row:
        row.value = value
    else:
        db.session.add(NodeSetting(key=key, value=value))


def is_enabled():
    return _get(ENABLED_KEY) != "false"


def set_enabled(enabled):
    _set(ENABLED_KEY, "true" if enabled else "false")
    db.session.commit()
    return bool(enabled)


def skipped_version():
    return _get(SKIPPED_KEY)


def skip_version(version):
    if parse_version(version) is None:
        raise ValueError("version must look like 1.2.3")
    _set(SKIPPED_KEY, version.strip().lstrip("vV"))
    db.session.commit()


# ── fetch ────────────────────────────────────────────────────────────────────

def manifest_url():
    return current_app.config.get("UPDATE_MANIFEST_URL") or DEFAULT_MANIFEST_URL


def fetch_manifest(url):
    """GET and parse the manifest. Raises on any problem. Refuses under
    TESTING so no test can reach the network by accident."""
    if current_app.config.get("TESTING"):
        raise RuntimeError("Network disabled under TESTING")
    if not url.lower().startswith("https://"):
        raise ValueError("manifest URL must be https")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S, context=SSL_CONTEXT) as r:
        raw = r.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("manifest too large")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest is not an object")
    return data


def _https(value):
    ok = (isinstance(value, str) and value.lower().startswith("https://")
          and len(value) <= MAX_URL_LEN)
    return value if ok else None


def _platform_key():
    if sys.platform == "darwin":
        return "mac_arm" if platform.machine().lower() in ("arm64", "aarch64") else "mac_intel"
    if sys.platform == "win32":
        return "windows"
    return None


def _reduce(manifest):
    """Keep only what this machine needs, so the stored value stays small."""
    version = manifest.get("version")
    if parse_version(version) is None:
        raise ValueError("manifest has no usable version")
    downloads = manifest.get("downloads")
    key = _platform_key()
    download = _https(downloads.get(key)) if key and isinstance(downloads, dict) else None
    return {"version": version.strip().lstrip("vV"),
            "notes_url": _https(manifest.get("notes_url")),
            "download_url": download}


# ── check ────────────────────────────────────────────────────────────────────

def _stored_result():
    try:
        data = json.loads(_get(LAST_RESULT_KEY) or "")
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def check(force=False):
    """Run a check if due (or forced) and return the stored result or None.
    Never raises. Needs an app context."""
    try:
        if not force:
            if not is_enabled():
                return _stored_result()
            try:
                last = float(_get(LAST_CHECK_KEY) or 0)
            except ValueError:
                last = 0
            if time.time() - last < CHECK_INTERVAL_S:
                return _stored_result()
        result = _reduce(fetch_manifest(manifest_url()))
        _set(LAST_RESULT_KEY, json.dumps(result, separators=(",", ":")))
        _set(LAST_CHECK_KEY, str(time.time()))
        db.session.commit()
        return result
    except Exception as e:  # silent to the user by design
        db.session.rollback()
        log.warning("update check failed: %s", e)
        try:
            return _stored_result()
        except Exception:
            return None


def status():
    """The payload the frontend reads. `available` already accounts for the
    skipped version; `skipped` says whether a newer one was skipped."""
    res = _stored_result() or {}
    latest = res.get("version")
    newer = bool(latest) and is_newer(latest, __version__)
    skipped = bool(newer) and skipped_version() == latest
    return {
        "current": __version__,
        "latest": latest,
        "available": bool(newer and not skipped),
        "notes_url": res.get("notes_url"),
        "download_url": res.get("download_url"),
        "skipped": skipped,
        "enabled": is_enabled(),
    }


def start_background(app):
    """One daemon thread, one check, after boot. Not under TESTING, not in
    SERVER_MODE."""
    if app.config.get("TESTING") or app.config.get("SERVER_MODE"):
        return None

    def run():
        time.sleep(STARTUP_DELAY_S)
        try:
            with app.app_context():
                check()
        except Exception as e:
            log.warning("update check thread failed: %s", e)

    t = threading.Thread(target=run, name="update-check", daemon=True)
    t.start()
    return t
