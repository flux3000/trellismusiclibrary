"""
config.py — Flux Audio application configuration.

Reads from environment variables when present (via .env),
falls back to safe defaults for local development.
"""

import os
import sys
from pathlib import Path

# The app's name lives in exactly one file. config.py needs it for the
# per-user data folder, and a second copy of a name is a second thing to
# forget to change. version.py imports nothing, so this cannot cycle.
from version import APP_NAME

# Base directory of this file
BASE_DIR = Path(__file__).parent.resolve()


def is_installed_app():
    """
    True when running as a packaged, double-clickable app rather than from a
    source checkout. PyInstaller sets sys.frozen on the bundle it builds.

    This is the ONLY thing that changes where data lives, and it is deliberately
    not a setting: a developer running `python3 run.py` must keep the exact
    layout they have always had, and an installed app must never write inside
    its own folder — that folder is sealed once the app is signed, and it is
    replaced wholesale by the next version.
    """
    return bool(getattr(sys, "frozen", False))


def resource_dir():
    """
    Where the app's read-only files live (app/static, fonts, the frontend).

    From source that is simply the repo. Inside a bundle PyInstaller unpacks
    them somewhere of its own choosing and tells us via sys._MEIPASS. Anything
    that builds a path to a shipped file must go through here, or it works in
    development and 404s the moment it is installed.
    """
    return Path(getattr(sys, "_MEIPASS", BASE_DIR))


def _default_data_dir():
    """
    Where THIS MACHINE'S library data lives — database, transcode cache.

    From source: the repo, unchanged, so nothing about a dev checkout moves.

    Installed: the per-user application-data location the platform expects
    (Ryan, 2026-08-25). It survives app updates, it is per-user on a shared
    machine, and backup software already knows about it. Windows and Linux
    are answered here too — not because either is supported yet, but because
    the alternative is discovering this function again later with a Windows
    user waiting.
    """
    if not is_installed_app():
        return BASE_DIR

    home = Path.home()
    # The FULL name (Ryan, 2026-08-25). This folder is something a person opens
    # in Finder looking for their library — "Trellis" alone is a word, "Trellis
    # Music Library" is an answer. The short form survives only where there is a
    # hard length budget: the macOS menu bar, and the wordmark in the app's own
    # header.
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local")
        return Path(base) / APP_NAME
    return (Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
            / APP_NAME.lower().replace(" ", "-"))


def _env_flag(name, default=False):
    """
    Parse a boolean environment variable. Only the literal string "true"
    (case-insensitive) is truthy; anything else (including unset, "false",
    "0", "") is False. `default` controls what an *unset* variable resolves
    to. Factored out so the DEV_MODE/SERVER_MODE default behavior is unit
    testable without needing to reload the config module.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() == "true"


# Known-insecure placeholder SECRET_KEY. Fine for a single-user local
# instance; app._validate_server_mode refuses to boot with this value
# when SERVER_MODE is on. Defined once here so config.py and
# app/__init__.py can't drift out of sync on what "the default" is.
DEV_SECRET_DEFAULT = "dev-secret-change-me"


class Config:
    # ── Security ──────────────────────────────────────────────
    SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_DEFAULT)

    # ── Database ──────────────────────────────────────────────
    # FLUX_DB_PATH exists for the two-node peer-sharing dev rig (2026-08-08):
    # a second instance needs its own database, and the path was hardcoded.
    # Prefixed (unlike LIBRARY_ROOT / IMPORT_DIR) on purpose — see FLUX_PORT.
    # Everything this machine WRITES hangs off DATA_DIR. Same relative layout
    # in both cases (db/, cache/), so only the root differs and no other code
    # has to know which mode it is in.
    #
    # The database file is `trellis.db` as of 2026-08-25 (Ryan: "we should not
    # be calling it fluxaudio.db"). The rename had been deferred; moving his
    # library into Application Support was the moment to stop deferring, since
    # it was being moved anyway.
    #
    # The ~20 scripts and tools that hardcode `db/fluxaudio.db` keep working:
    # the repo's db/ holds symlinks under BOTH names pointing at the one real
    # file. A symlink's name has nothing to do with its target's.
    DATA_DIR = Path(os.environ.get("TRELLIS_DATA_DIR") or _default_data_dir())

    DB_PATH = Path(os.environ.get("FLUX_DB_PATH") or (DATA_DIR / "db" / "trellis.db"))

    # utils/transcode.py has always honoured this key and fallen back to a path
    # relative to the app package. That fallback lands INSIDE the bundle once
    # installed, which is read-only — so the key is now actually set.
    TRANSCODE_CACHE_DIR = (os.environ.get("TRANSCODE_CACHE_DIR")
                           or str(DATA_DIR / "cache" / "transcodes"))
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{DB_PATH}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # SQLite: increase timeout for long-running analysis writes (default 5s is too short)
    SQLALCHEMY_ENGINE_OPTIONS = {
        "connect_args": {"timeout": 60, "check_same_thread": False}
    }

    # ── Library ───────────────────────────────────────────────
    # Root directory where all audio recordings are stored.
    # Tracks are referenced by ID; this path never leaves the server.
    # "Trellis" (Ryan, 2026-08-23) — the NAS folder itself was renamed on disk
    # from "Flux Audio" to "Trellis"; these defaults follow it. See
    # project_app_naming.md for the naming decision. (The dev repo/app root at
    # ~/Workshop/dev/trellis and the DB filename are a separate, deliberately
    # deferred rename — see that memo's "Rebrand blast radius" section.)
    LIBRARY_ROOT = Path(os.environ.get(
        "LIBRARY_ROOT",
        "/Volumes/music/Trellis/Library"
    ))

    # Allowlist of base directories the ingest-preview endpoint is permitted
    # to read from. `folder` query params are resolved and must fall inside
    # one of these roots — otherwise arbitrary filesystem reads would be
    # possible for any logged-in user. Override via IMPORT_ROOTS env var
    # (":"-separated list of paths).
    #
    # Widened 2026-08-31 (Ryan) to include the home directory: Add Recordings
    # was refusing any source folder outside LIBRARY_ROOT/Volumes with an
    # unexplained 403, which meant an existing folder anywhere under ~ (an
    # external drive aside) could never be used as a source. Still bounded —
    # this is not "any path the process can read" — just widened to cover
    # where a single-user Mac actually keeps files.
    _import_roots_env = os.environ.get("IMPORT_ROOTS", "").strip()
    if _import_roots_env:
        IMPORT_ROOTS = [p for p in _import_roots_env.split(":") if p]
    else:
        IMPORT_ROOTS = [str(LIBRARY_ROOT), "/Volumes", str(Path.home())]

    # Where new material lands before ingest. This is the folder every "pick a
    # folder" box should open to. Defined here rather than hardcoded in the
    # frontend so there is one place to change it — it has moved three times
    # now (Live Music Archive/Workshop/Import -> Flux Workshop/Download ->
    # Flux Audio/Download, 2026-08-13 -> Trellis/Download, 2026-08-23) and an
    # old value was once baked into app.js.
    IMPORT_DIR = os.environ.get(
        "IMPORT_DIR",
        "/Volumes/music/Trellis/Download"
    )

    # Triage destinations. During Listening Quality triage a show can be moved
    # OUT of the ingest queue rather than ingested — either because the quality
    # isn't good enough to bother with, or because it needs work first. These
    # are real folders on disk (siblings of IMPORT_DIR); moving is a physical
    # move, so the show leaves the scanned directory and stops being offered.
    TRIAGE_DIRS = {
        "backlog": os.environ.get(
            "BACKLOG_DIR", "/Volumes/music/Trellis/Backlog"),
        "workshop": os.environ.get(
            "WORKSHOP_DIR", "/Volumes/music/Trellis/Workshop"),
    }

    # ── App ───────────────────────────────────────────────────
    # Note: Flask's own debug reloader is always forced off under PyWebView
    # (see run.py), so no DEBUG flag is carried here.
    # Cookies are scoped by HOST, not by port — 127.0.0.1:5757 and
    # 127.0.0.1:5758 share one cookie jar. With both nodes of the peer-sharing
    # rig naming their cookie `session`, each node's Set-Cookie overwrites the
    # other's, and (since both DBs carry the same admin user id) a session
    # minted by one node authenticates against the other. Give each node its
    # own cookie name — and its own SECRET_KEY — and the two stop colliding.
    # Irrelevant in production, where nodes are distinct hosts.
    SESSION_COOKIE_NAME = os.environ.get("FLUX_COOKIE_NAME") or "session"

    HOST  = "127.0.0.1"
    # FLUX_PORT, not PORT: a bare PORT is set by all sorts of tooling and
    # shells, and a node silently binding somewhere other than 5757 is a
    # miserable thing to debug. The prefix costs nothing and can't collide.
    PORT  = int(os.environ.get("FLUX_PORT") or 5757)   # internal Flask port used by PyWebView

    # ── Dev mode ──────────────────────────────────────────────
    # When True, skips login entirely — auto-logs in the first admin user.
    # Defaults to FALSE (fail-closed): must explicitly opt in with
    # DEV_MODE=true for local development. Never enable alongside SERVER_MODE.
    DEV_MODE = _env_flag("DEV_MODE", default=False)

    # ── Server mode ───────────────────────────────────────────
    # When True, indicates the app is running on a shared/public box rather
    # than a single-user local machine. Tightens boot-time validation
    # (see app._validate_server_mode): refuses to start with DEV_MODE on or
    # with a default/blank SECRET_KEY.
    SERVER_MODE = _env_flag("SERVER_MODE", default=False)

    # ── Single-user desktop ───────────────────────────────────
    # An installed app on someone's own Mac has nothing to log in to: one
    # person, one machine, no remote access to the front door. Requiring a
    # password there is friction protecting nothing (Ryan, 2026-08-25 — first
    # run should just open).
    #
    # Deliberately NOT reusing DEV_MODE, which happens to do the same
    # auto-login: DEV_MODE also turns on developer debug logging, and a flag
    # whose name lies about why it is set is how the wrong one gets enabled in
    # the wrong place. app._validate_server_mode refuses this alongside
    # SERVER_MODE for exactly the reason it refuses DEV_MODE.
    SINGLE_USER_DESKTOP = _env_flag(
        "SINGLE_USER_DESKTOP", default=is_installed_app())

    # ── Peer sharing identity (2026-08-08) ────────────────────
    # api/peers.py and api/share.py already read these; until now nothing
    # DEFINED them, so `mint_invite` returned `invite: null` and there was no
    # way to hand a peer the single `address#code` string the design calls for.
    #
    # SHARE_BASE_URL is the address a peer's app will hit — the Cloudflare
    # Tunnel hostname once that exists. Deliberately unset by default: an
    # invite string containing a wrong address is worse than no invite string,
    # because it fails at the peer's end with nothing to point at. The admin UI
    # shows the raw code and explains what's missing instead.
    SHARE_BASE_URL = os.environ.get("SHARE_BASE_URL") or None

    # ── User avatars ──────────────────────────────────────────
    # Beside the database, NOT under LIBRARY_ROOT (Ryan, 2026-08-25). Performer
    # and venue photos live in the music library because they are about its
    # contents; a person's own picture is about the INSTALL. A listener has no
    # music library at all — putting an avatar there would mean Jeff cannot have
    # a face.
    # Derived from the DATABASE, not from DATA_DIR, and RESOLVED through any
    # symlink (2026-08-25). Ryan's library file lives in Application Support
    # with a symlink left at the old repo path so the twenty-odd scripts that
    # hardcode it keep working. Without .resolve() a source run would put
    # pictures in the repo and the installed app would put them in Application
    # Support — one library, two faces, and neither app showing the other's.
    #
    # Following the database also gets the rigs right for free: a consumer node
    # pointed at its own database gets its own avatars, which is correct — it is
    # a different person's install.
    AVATAR_DIR = os.environ.get("AVATAR_DIR") or str(
        Path(DB_PATH).resolve().parent.parent / "avatars")

    # ── ffmpeg ────────────────────────────────────────────────
    # Full path to ffmpeg, when it is somewhere unusual. Normally unset:
    # utils/transcode.resolve_ffmpeg() checks PATH and the usual Homebrew
    # locations. Set this if a packaged app cannot find it — a double-clicked
    # app gets a minimal PATH that excludes /opt/homebrew/bin.
    FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or None

    # ── Peer-door rate limiting (2026-08-25) ──────────────────
    # /api/share/enroll is the only route reachable with no credentials, and
    # the only one an internet stranger can call. See app/utils/rate_limit.py.
    # Ten tries per caller per fifteen minutes: generous for a person fumbling
    # a pasted invite, useless for a script.
    ENROLL_RATE_LIMIT  = int(os.environ.get("ENROLL_RATE_LIMIT", 10))
    ENROLL_RATE_WINDOW = int(os.environ.get("ENROLL_RATE_WINDOW", 900))

    # Set to "CF-Connecting-IP" when running behind a Cloudflare Tunnel.
    # Unset by default and DELIBERATELY not defaulted: cloudflared connects
    # from 127.0.0.1, so without this every visitor on earth shares one
    # rate-limit bucket — but trusting a client-supplied header on an install
    # that has no proxy in front of it is worse. Only honoured when the
    # immediate peer is loopback.
    TRUSTED_CLIENT_IP_HEADER = os.environ.get("TRUSTED_CLIENT_IP_HEADER") or None

    # How this node introduces itself on enroll and /api/share/me. The owner
    # falls back to the first admin's username when unset (see share.py).
    # "Trellis Library" (Ryan, 2026-08-23) — the rebrand's default share name;
    # was "Flux Library". See project_app_naming.md for the naming decision.
    # Deliberately None when unset, NOT a shared default string (2026-08-24).
    # "Trellis Library" as a fallback meant every install on earth introduced
    # itself with the identical name, so a collector who joined three friends'
    # libraries saw three indistinguishable entries in their selector. The name
    # is now DERIVED from the owner in share._node_identity(), which can reach
    # the admin user that config cannot.
    SHARE_NODE_NAME = os.environ.get("SHARE_NODE_NAME") or None
    SHARE_OWNER_NAME = os.environ.get("SHARE_OWNER_NAME") or None
