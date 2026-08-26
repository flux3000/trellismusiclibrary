"""
run.py — Flux Audio entry point.

Starts Flask in a background thread and opens a PyWebView window.
Exposes a Python API to JavaScript for native OS interactions
(e.g. folder picker) that the browser alone cannot perform.

Usage:
    python run.py
"""

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import webview
from app import create_app
from config import Config
from version import APP_NAME

app = create_app()


def first_run_setup():
    """
    Make an empty machine usable, once.

    An installed app cannot ask someone to run a setup script — the whole point
    is that it opens when you double-click it. So: if there is no database yet,
    create the schema and the owner account, and open straight into an empty
    library (Ryan, 2026-08-25 — first run should just open).

    Runs ONLY when the database file is absent. An existing checkout is left
    completely alone: this must never become a substitute for a migration
    script, and create_all() cannot add a column to a table that already
    exists anyway. If it fired every boot it would quietly conjure tables for
    half-finished models and hide the fact that a migration was skipped.

    The owner's password is random and thrown away. Nobody types it: the
    desktop app signs the owner in automatically because there is nothing to
    log in to on your own machine. If this database is ever pointed at a shared
    node, set a real password first — scripts/init_db.py is the way in.
    """
    if Config.DB_PATH.exists():
        return

    import secrets
    import getpass

    import importlib

    import bcrypt
    from app.extensions import db
    from app.models.user import User

    # NOT `import app.models`. That statement binds the name `app` in this
    # function's scope to the PACKAGE, shadowing the Flask instance defined
    # above — and the failure lands two lines later on `app.app_context()`
    # with a message about a module having no such attribute, which reads like
    # a broken install rather than a shadowed name. Caught by running this;
    # every static check passed it.
    importlib.import_module("app.models")   # registers every model with SQLAlchemy

    Config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with app.app_context():
        db.create_all()
        if db.session.query(User).first() is None:
            try:
                who = (getpass.getuser() or "").strip() or "owner"
            except Exception:
                who = "owner"
            db.session.add(User(
                username      = who,
                password_hash = bcrypt.hashpw(
                    secrets.token_urlsafe(32).encode(), bcrypt.gensalt()).decode(),
                role          = "admin",
                all_artists   = True,
                is_active     = True,
            ))
            db.session.commit()
    print(f"First run: created a new library at {Config.DB_PATH}")


# -- Window geometry ----------------------------------------------------------
#
# Lives beside the database rather than in the OS application-support directory:
# `db/` is already where this app keeps its local, per-machine, never-committed
# state, and one convention beats two. It is deliberately NOT a UserPreference
# row -- window size is per-machine (a laptop and a desktop want different
# answers from the same account) and it is needed before Flask is serving, let
# alone before anyone has logged in.
# 2026-08-25: hangs off Config.DATA_DIR rather than the source folder. The
# reasoning below is unchanged — this still lives beside the database, which is
# still where per-machine state belongs. What changed is that "beside the
# database" is a writable per-user directory once the app is installed, because
# an installed app's own folder is sealed.
WINDOW_STATE_PATH = Config.DATA_DIR / "db" / "window_state.json"

MIN_W, MIN_H = 960, 640

# The DEFAULT width is capped; the height is not (Ryan, 2026-08-22 — 88% of an
# ultrawide is a window nobody wants, but 88% of any screen's height is fine).
# 1760 is about a large laptop's full width, which is as wide as this layout has
# anything to say: the sidebar is fixed, the search field is 620px, and past
# roughly here the Track List is just growing whitespace between a title and its
# duration. Only the default is capped — if you deliberately drag the window
# wider, that is remembered as-is.
MAX_DEFAULT_W = 1760


def _screen_size():
    """Primary screen in logical pixels, or None if PyWebView cannot say."""
    try:
        screen = webview.screens[0]
        return int(screen.width), int(screen.height)
    except Exception:
        return None


def _default_geometry():
    """
    Most of the screen, not a fixed number. The old 1440x900 default was picked
    for one display and read as cramped on anything larger (Ryan, 2026-08-22) --
    and a bigger fixed number would simply be wrong in the other direction on a
    laptop. 88% leaves the dock and menu bar clear.

    Width is then clamped to MAX_DEFAULT_W. Height is not: vertical space is
    always useful here (it is more track rows), horizontal space past a point is
    not.
    """
    screen = _screen_size()
    if not screen:
        return min(1600, MAX_DEFAULT_W), 1000
    w, h = screen
    width = min(max(MIN_W, int(w * 0.88)), MAX_DEFAULT_W)
    return width, max(MIN_H, int(h * 0.88))


def load_window_size():
    """
    Last used size, or the default. Validated rather than trusted: a size saved
    on a large external display and restored on the laptop alone would open a
    window bigger than the screen with its controls off the edge, which looks
    like a crash. Anything unreadable, undersized or oversized falls back.
    """
    default = _default_geometry()
    try:
        data = json.loads(WINDOW_STATE_PATH.read_text(encoding="utf-8"))
        w, h = int(data["width"]), int(data["height"])
    except Exception:
        return default
    if w < MIN_W or h < MIN_H:
        return default
    screen = _screen_size()
    if screen and (w > screen[0] or h > screen[1]):
        return default
    return w, h


def save_window_size(width, height):
    """Best-effort -- never let a failed write stop the app from closing."""
    try:
        WINDOW_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        WINDOW_STATE_PATH.write_text(
            json.dumps({"width": int(width), "height": int(height)}), encoding="utf-8")
    except Exception:
        pass


class FluxAPI:
    """
    Python functions exposed to the frontend via window.pywebview.api.*
    Called from JavaScript as async functions — always return JSON-safe values.
    """

    def pick_folder(self):
        """
        Open a native macOS folder picker dialog.
        Returns the selected folder path as a string, or None if cancelled.
        Called from JS: const path = await window.pywebview.api.pick_folder()
        """
        result = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER)
        if result:
            return result[0]
        return None

    def get_library_root(self):
        """Return the configured library root path (for display purposes only)."""
        return str(Config.LIBRARY_ROOT)

    def open_in_browser(self, url):
        """
        Open a URL in the system's default browser (macOS: `open`).
        Used by the debug panel pop-out since PyWebView blocks window.open().
        Called from JS: await window.pywebview.api.open_in_browser(url)
        """
        try:
            subprocess.Popen(['open', url])
            return True
        except Exception as e:
            return str(e)


def start_flask():
    """
    Run Flask in a background thread so PyWebView owns the main thread.

    threaded=True (2026-07-19): the dev server otherwise handles ONE request
    at a time — a single slow request (a Batch Import "Review" scan hitting
    a slow NAS read, for example) blocks the ENTIRE app, including unrelated
    UI actions and even the debug panel's own polling. That's what made a
    stuck scan look like "the whole app died" rather than "one request is
    slow" — and why "New Scan" never helped: the retry just queued up behind
    the same stuck worker. config.py already sets check_same_thread=False on
    the SQLite connection specifically to support this.
    """
    app.run(
        host         = Config.HOST,
        port         = Config.PORT,
        debug        = False,       # must be False under PyWebView
        use_reloader = False,
        threaded     = True,
    )


if __name__ == "__main__":
    # ── Startup self-test ─────────────────────────────────────────────────────
    # `TRELLIS_SELFTEST=1 Trellis` starts everything the app needs and exits
    # without opening a window. tools/build_macos.sh runs this against the
    # freshly built bundle.
    #
    # It exists because the interesting packaging failures happen at IMPORT
    # time, before there is a window or a log to look at: a data file the
    # packager did not know to copy, a module it could not see. From Finder
    # that looks like nothing happening at all. By the time create_app() has
    # returned and first_run_setup() has built a database, every module has
    # been imported and every import-time data file has been read — which is
    # precisely the class of bug that shipped a broken bundle on 2026-08-25
    # (geonamescache's JSON tables).
    if os.environ.get("TRELLIS_SELFTEST") == "1":
        first_run_setup()
        print(f"selftest ok — {Config.DB_PATH}")
        sys.exit(0)

    # Ctrl-C should kill the process even while PyWebView owns the main thread
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    # Before anything serves a request: an empty machine gets a library.
    first_run_setup()

    # Flask runs in a daemon thread — dies when the window closes
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # PyWebView must own the main thread on macOS
    start_w, start_h = load_window_size()
    window = webview.create_window(
        title    = APP_NAME,
        url      = f"http://{Config.HOST}:{Config.PORT}",
        js_api   = FluxAPI(),
        width    = start_w,
        height   = start_h,
        min_size = (MIN_W, MIN_H),
    )

    # Track the size as it changes and write it once on the way out, rather than
    # writing on every resize event -- a single drag fires those continuously.
    # `resized` is the source of truth: window.width/height are not guaranteed
    # to be refreshed by the time `closing` runs on every backend, so the last
    # event we saw is trusted over re-reading the object. Both subscriptions are
    # wrapped because these event names have moved between PyWebView releases,
    # and a window that will not open is a far worse bug than one that forgets
    # how big it was.
    latest = {"w": start_w, "h": start_h}

    def _on_resized(width, height):
        latest["w"], latest["h"] = width, height

    def _on_closing():
        save_window_size(latest["w"], latest["h"])

    try:
        window.events.resized += _on_resized
    except Exception:
        pass
    try:
        window.events.closing += _on_closing
    except Exception:
        pass

    webview.start()

    # Belt and braces: if `closing` never fired -- the event is missing on this
    # backend, or the window went away another way -- start() still returns on a
    # normal quit, and the last size we saw gets written here.
    save_window_size(latest["w"], latest["h"])
