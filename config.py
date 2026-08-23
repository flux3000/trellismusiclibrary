"""
config.py — Flux Audio application configuration.

Reads from environment variables when present (via .env),
falls back to safe defaults for local development.
"""

import os
from pathlib import Path

# Base directory of this file
BASE_DIR = Path(__file__).parent.resolve()


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
    DB_PATH = Path(os.environ.get("FLUX_DB_PATH") or (BASE_DIR / "db" / "fluxaudio.db"))
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
    # ~/Workshop/dev/fluxaudio and the DB filename are a separate, deliberately
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
    _import_roots_env = os.environ.get("IMPORT_ROOTS", "").strip()
    if _import_roots_env:
        IMPORT_ROOTS = [p for p in _import_roots_env.split(":") if p]
    else:
        IMPORT_ROOTS = [str(LIBRARY_ROOT), "/Volumes"]

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

    # How this node introduces itself on enroll and /api/share/me. The owner
    # falls back to the first admin's username when unset (see share.py).
    # "Trellis Library" (Ryan, 2026-08-23) — the rebrand's default share name;
    # was "Flux Library". See project_app_naming.md for the naming decision.
    SHARE_NODE_NAME = os.environ.get("SHARE_NODE_NAME") or "Trellis Library"
    SHARE_OWNER_NAME = os.environ.get("SHARE_OWNER_NAME") or None
