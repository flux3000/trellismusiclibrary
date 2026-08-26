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


# ── First-run library location (2026-08-26) ──────────────────────────────
#
# config.py's LIBRARY_ROOT default is Ryan's own NAS mount -- correct on the
# machine that mount was built for, meaningless anywhere else. Before this,
# a fresh install just reported "library drive not mounted" and stopped,
# with no way forward -- confirmed on a brand-new Mac Mini with none of that
# infrastructure. Every other install has the identical problem.
#
# TRELLIS_ROOT_MARKER remembers a chosen location across launches. It lives
# beside the database (Config.DATA_DIR), never inside the library itself --
# the marker has to be readable before we know where the library even is.
TRELLIS_ROOT_MARKER = Config.DATA_DIR / "trellis_root.json"
TRELLIS_SUBFOLDERS  = ("Library", "Download", "Backlog", "Workshop")


def _read_trellis_root_marker():
    """The Trellis folder chosen on a previous run, or None."""
    try:
        data = json.loads(TRELLIS_ROOT_MARKER.read_text(encoding="utf-8"))
        root = data.get("trellis_root")
        return Path(root) if root else None
    except Exception:
        return None


def _write_trellis_root_marker(root):
    TRELLIS_ROOT_MARKER.parent.mkdir(parents=True, exist_ok=True)
    TRELLIS_ROOT_MARKER.write_text(
        json.dumps({"trellis_root": str(root)}), encoding="utf-8")


def _looks_reachable(path):
    """
    Cheap, synchronous "can we list this" check -- good enough for a
    one-time startup decision. Deliberately not the full library_mount.py
    treatment (a threaded probe with a timeout, built for a mount that can
    hang mid-request): this runs once, before the window even opens, so a
    genuinely hung mount here just delays launch rather than wedging a live
    Flask worker.
    """
    try:
        os.listdir(path)
        return True
    except Exception:
        return False


def _apply_trellis_root(root):
    """Point LIBRARY_ROOT/IMPORT_DIR/TRIAGE_DIRS at <root>'s four folders."""
    app.config["LIBRARY_ROOT"] = str(root / "Library")
    app.config["IMPORT_DIR"]   = str(root / "Download")
    app.config["TRIAGE_DIRS"]  = {
        "backlog":  str(root / "Backlog"),
        "workshop": str(root / "Workshop"),
    }


def resolve_trellis_root_and_patch_config():
    """
    Decide where the library lives for THIS run and patch it into
    app.config -- a live dict Flask already built from Config by the time
    this runs, so editing Config itself here would do nothing.

    Returns True if the app can go straight to its main window, False if
    first-run setup (the folder picker) has to run first.
    """
    # An explicit env var always wins -- the two-node dev rig and any
    # headless deployment depend on this, unchanged.
    if os.environ.get("LIBRARY_ROOT"):
        return True

    marker = _read_trellis_root_marker()
    if marker is not None:
        _apply_trellis_root(marker)
        return True

    # Nothing chosen yet. If the old hardcoded default happens to be
    # reachable right now (Ryan's NAS-mounted dev machine), keep working
    # exactly as it always has -- this machine never needs the picker.
    if _looks_reachable(Config.LIBRARY_ROOT):
        return True

    return False


def _setup_html():
    """
    The one-time first-run screen. Inline, not a static file -- Flask isn't
    serving anything yet at this point, and this only ever runs once per
    machine. window.location.href navigates this same window over to the
    real app once confirm_trellis_root() succeeds -- ordinary web navigation,
    nothing PyWebView-specific needed for that part.
    """
    app_url = f"http://{Config.HOST}:{Config.PORT}"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; height: 100vh; display: flex; align-items: center;
    justify-content: center; background: #14161a; color: #e8e6e1;
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
  }}
  .card {{ max-width: 460px; padding: 40px; }}
  h1 {{ font-size: 21px; margin: 0 0 16px; }}
  p {{ font-size: 14px; line-height: 1.6; color: #b8b5ae; margin: 0 0 12px; }}
  .folders {{
    font-family: ui-monospace, "JetBrains Mono", monospace; font-size: 13px;
    color: #d8d5ce; background: #1e2126; border-radius: 6px;
    padding: 12px 16px; margin: 0 0 20px;
  }}
  button {{
    font-size: 14px; padding: 10px 20px; border-radius: 6px; border: none;
    background: #d98f4e; color: #14161a; font-weight: 600; cursor: pointer;
  }}
  button:disabled {{ opacity: .5; cursor: default; }}
  .chosen {{ margin-top: 14px; font-size: 13px; color: #8fbf7f; word-break: break-all; }}
  .err {{ margin-top: 14px; font-size: 13px; color: #e0806a; }}
  label {{ display: block; font-size: 13px; color: #b8b5ae; margin: 4px 0 6px; }}
  input.field {{
    width: 100%; font-size: 14px; padding: 9px 12px; margin-bottom: 20px;
    border-radius: 6px; border: 1px solid #3a3d43; background: #1e2126;
    color: #e8e6e1; font-family: inherit;
  }}
  input.field:focus {{ outline: none; border-color: #d98f4e; }}
</style></head>
<body><div class="card">
  <h1>Welcome to Trellis</h1>
  <label for="username">What should we call you?</label>
  <input id="username" class="field" type="text" placeholder="e.g. jeff" autocomplete="off">
  <p>Now, choose a location and Trellis will create a <strong>Trellis</strong>
     folder there, with four folders inside it:</p>
  <div class="folders">Download &mdash; where new recordings land<br>
  Workshop &mdash; needs work before it's ready<br>
  Backlog &mdash; set aside during triage<br>
  Library &mdash; your collection</div>
  <p>You can change the folder later. For now, pick where it should start.</p>
  <button id="choose">Choose Location&hellip;</button>
  <div id="status"></div>
</div>
<script>
  const btn = document.getElementById('choose')
  const status = document.getElementById('status')
  const usernameInput = document.getElementById('username')
  btn.addEventListener('click', async () => {{
    const username = usernameInput.value.trim()
    if (!username) {{
      status.className = 'err'
      status.textContent = 'Tell us what to call you first.'
      usernameInput.focus()
      return
    }}
    btn.disabled = true
    status.className = ''
    status.textContent = ''
    try {{
      const parent = await window.pywebview.api.pick_folder()
      if (!parent) {{ btn.disabled = false; return }}
      status.textContent = 'Setting up your library\u2026'
      const result = await window.pywebview.api.confirm_trellis_root(parent, username)
      if (result && result.ok) {{
        status.className = 'chosen'
        status.textContent = 'Created ' + result.root
        window.location.href = {app_url!r}
      }} else {{
        status.className = 'err'
        status.textContent = (result && result.error) || 'Something went wrong. Try again.'
        btn.disabled = false
      }}
    }} catch (e) {{
      status.className = 'err'
      status.textContent = String(e)
      btn.disabled = false
    }}
  }})
</script></body></html>"""


def first_run_setup(create_default_user=True):
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

    create_default_user=False (2026-08-26) on a genuinely fresh install,
    where the setup page is about to ask the person for a name instead of
    settling for whatever their OS account is called. In that case this
    still creates the schema — the User table has to exist before anyone can
    be inserted into it — it just leaves the table empty. The account gets
    created once, under the chosen name, by FluxAPI.confirm_trellis_root()
    when the setup page's form is submitted.
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
        if create_default_user and db.session.query(User).first() is None:
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

    def confirm_trellis_root(self, parent_path, username=None):
        """
        First-run only. The user picked a parent folder via pick_folder() and
        typed a name for themselves; this creates
        <parent>/Trellis/{Library,Download,Backlog,Workshop}, remembers the
        folder choice for next launch, patches the already-running app's
        config -- Flask started before this could possibly be known -- and
        creates the owner account under the chosen name. Called from the
        setup page's JS.

        The account only gets created here on a genuinely empty database --
        first_run_setup() skips its own automatic account for exactly this
        case (its create_default_user flag). Anywhere that already has an
        account, including the "old default happened to be reachable" case
        that skips this page entirely, this leaves it alone rather than
        renaming it out from under someone.

        Returns {"ok": True, "root": "..."} or {"ok": False, "error": "..."}.
        """
        try:
            root = Path(parent_path) / "Trellis"
            for name in TRELLIS_SUBFOLDERS:
                (root / name).mkdir(parents=True, exist_ok=True)
            _write_trellis_root_marker(root)
            _apply_trellis_root(root)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        try:
            self._create_owner_account(username)
        except Exception as e:
            return {"ok": False, "error": f"Folder created, but account setup failed: {e}"}

        return {"ok": True, "root": str(root)}

    def _create_owner_account(self, username):
        """
        Create the owner account under `username` -- same random-password,
        thrown-away-on-the-spot, auto-login design as first_run_setup(); see
        that docstring for why nobody ever types a password on this machine.

        No-ops if an account already exists, so a retried submission (say,
        the folder step succeeded but this failed the first time) never
        tries to create a second one.
        """
        import secrets
        import bcrypt
        from app.extensions import db
        from app.models.user import User

        with app.app_context():
            if db.session.query(User).first() is not None:
                return
            who = (username or "").strip()[:64] or "owner"
            db.session.add(User(
                username      = who,
                password_hash = bcrypt.hashpw(
                    secrets.token_urlsafe(32).encode(), bcrypt.gensalt()).decode(),
                role          = "admin",
                all_artists   = True,
                is_active     = True,
            ))
            db.session.commit()

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

    # Where does the library actually live on THIS machine? Patches
    # app.config in place. Comes back False only on a genuinely fresh
    # install with nothing configured and nothing reachable at the old
    # default -- the window opens on the setup page instead of the real
    # app until a folder is chosen. Resolved BEFORE first_run_setup() so the
    # latter knows whether a person is about to type a name into that page,
    # or whether this machine is skipping straight to the main window and
    # needs its usual automatic owner account.
    trellis_root_ready = resolve_trellis_root_and_patch_config()

    # Before anything serves a request: an empty machine gets a database.
    # The default owner account is only created here when the setup page
    # isn't about to ask for a name instead -- see confirm_trellis_root().
    first_run_setup(create_default_user=trellis_root_ready)

    # Flask runs in a daemon thread — dies when the window closes
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # PyWebView must own the main thread on macOS
    start_w, start_h = load_window_size()
    window_kwargs = dict(
        title    = APP_NAME,
        js_api   = FluxAPI(),
        width    = start_w,
        height   = start_h,
        min_size = (MIN_W, MIN_H),
    )
    if trellis_root_ready:
        window = webview.create_window(url=f"http://{Config.HOST}:{Config.PORT}", **window_kwargs)
    else:
        window = webview.create_window(html=_setup_html(), **window_kwargs)

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
