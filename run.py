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
TRELLIS_ROOT_MARKER     = Config.DATA_DIR / "trellis_root.json"
# The folder name a person sees in Finder, chosen 2026-08-26 (Ryan): the
# short "Trellis" collided visually with the code repo of the same name.
TRELLIS_ROOT_FOLDER_NAME = "Trellis Music Library"
# "Downloads", not "Download" (2026-10-01): matches the page and nav name.
TRELLIS_SUBFOLDERS       = ("Library", "Downloads", "Backlog", "Workshop")


def _read_trellis_root_marker():
    """
    What was chosen on a previous run, as a dict, or None.

    Two shapes, because there are now two ways to answer the first-run
    question (2026-09-17):

      {"mode": "created",  "trellis_root": "..."}
          Trellis laid the folder out itself: <root>/{Library, Downloads,
          Backlog, Workshop}. The original and still the default.

      {"mode": "imported", "library_root": "...",
       "import_dir": ... | None, "backlog_dir": ... | None,
       "workshop_dir": ... | None}
          The user pointed Trellis at a library they already had. The three
          working folders are OPTIONAL here (Ryan, 2026-09-17) -- a collector
          may simply not have a Backlog, and inventing one inside their
          collection is not ours to do.

    A marker written before this existed has no "mode" and only
    "trellis_root". That is the created shape, and it must keep resolving
    exactly as it always has -- an existing install cannot be asked to
    re-answer a question because a key appeared.
    """
    try:
        data = json.loads(TRELLIS_ROOT_MARKER.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("mode") == "imported":
        return data if data.get("library_root") else None
    return data if data.get("trellis_root") else None


def _write_trellis_root_marker(root):
    _write_marker({"mode": "created", "trellis_root": str(root)})


def _write_marker(data):
    TRELLIS_ROOT_MARKER.parent.mkdir(parents=True, exist_ok=True)
    TRELLIS_ROOT_MARKER.write_text(json.dumps(data), encoding="utf-8")


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


def _paths_overlap(a, b):
    """True when a equals, contains, or sits inside b (realpaths)."""
    a, b = a.rstrip(os.sep) or os.sep, b.rstrip(os.sep) or os.sep
    return (a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)
            or a == os.sep or b == os.sep)


def _apply_trellis_root(root, import_dir=None, backlog_dir=None, workshop_dir=None):
    """
    Point LIBRARY_ROOT/IMPORT_DIR/TRIAGE_DIRS/IMPORT_ROOTS at <root>'s four
    folders.

    IMPORT_ROOTS (2026-08-26) matters just as much as the other three and is
    easy to forget: it's the allowlist every filesystem-reading endpoint
    checks a folder against before it will browse, import, triage-move, or
    stream from it (app/api/quality.py, app/api/stream.py) -- config.py
    computes it once, from the OLD hardcoded default, at class-definition
    time. Patching the other three keys without this one means "Add
    Recordings" (and playback) rejects every folder under a freshly chosen
    root as "outside the permitted import roots," having never heard of it.
    Set to the whole Trellis root, not just Library, since Downloads/Backlog/
    Workshop all need to pass this same check. "/Volumes" is kept for parity
    with the original default -- browsing in from an external drive should
    still work.
    """
    app.config["LIBRARY_ROOT"] = str(root / "Library")
    # The three working folders default to <root>'s own subfolders, but any
    # of them can be pointed elsewhere from Settings (Ryan, 2026-10-01: always
    # editable, created library or imported). An override is stored in the
    # marker under the same keys an imported library uses.
    app.config["IMPORT_DIR"]   = str(import_dir or (root / "Downloads"))
    app.config["TRIAGE_DIRS"]  = {
        "backlog":  str(backlog_dir or (root / "Backlog")),
        "workshop": str(workshop_dir or (root / "Workshop")),
    }
    roots = [str(root), "/Volumes"]
    roots += [str(d) for d in (import_dir, backlog_dir, workshop_dir) if d]
    app.config["IMPORT_ROOTS"] = roots


def _apply_imported_library(library_root, import_dir=None,
                            backlog_dir=None, workshop_dir=None):
    """
    Point the app at a library the user ALREADY had (2026-09-17).

    The difference from _apply_trellis_root is that nothing is derived and
    nothing is created. LIBRARY_ROOT is the folder they picked, full stop --
    not <picked>/Library, because their library IS that folder and a Library
    subfolder inside it would be a second, empty one.

    The three working folders are OPTIONAL and independent. A destination
    that was not configured is LEFT OUT of TRIAGE_DIRS entirely rather than
    pointed somewhere invented: both readers (api/quality.py::triage-move and
    api/recordings.py::move-out) validate membership against this dict, so an
    absent key is already a clean refusal rather than a move into a folder
    that silently gets created on someone's disk.

    IMPORT_ROOTS gets the library plus whichever working folders exist. It is
    the allowlist every filesystem-reading endpoint checks before it will
    browse, import, triage-move or stream -- forgetting it is what made "Add
    Recordings" reject every folder under a freshly chosen root in 2026-08.
    """
    library_root = str(library_root)
    app.config["LIBRARY_ROOT"] = library_root

    triage = {}
    if backlog_dir:
        triage["backlog"] = str(backlog_dir)
    if workshop_dir:
        triage["workshop"] = str(workshop_dir)
    app.config["TRIAGE_DIRS"] = triage

    # No Downloads folder set means ~/Downloads/Trellis (2026-10-01). It used
    # to be the library root, which made Add Recordings open on the whole
    # collection and gave archive downloads nowhere to land. Not created here:
    # a permission prompt for ~/Downloads at launch is bad; downloads_dir.
    # ensure_downloads_dir() creates it on first need.
    # A saved Downloads folder that is the library itself (or inside it) is a
    # leftover from before Downloads had its own default: Add Recordings would
    # open on the collection. Treat it as unset.
    if import_dir and _paths_overlap(os.path.realpath(str(import_dir)),
                                     os.path.realpath(library_root)):
        import_dir = None
    if import_dir:
        eff_import = str(import_dir)
    else:
        from app.utils.downloads_dir import default_downloads_dir
        eff_import = default_downloads_dir()
    app.config["IMPORT_DIR"] = eff_import

    roots = [library_root, "/Volumes", eff_import]
    roots += [str(d) for d in (import_dir, backlog_dir, workshop_dir) if d and str(d) != eff_import]
    app.config["IMPORT_ROOTS"] = roots


def _maybe_start_bulk_ingest_from_marker():
    """
    Bulk Ingest (spec 1.9/4, chunk 7): if the marker says
    ingest_existing (set by confirm_existing_library() when someone answers
    "Use a folder I already have"), start a run over the configured
    LIBRARY_ROOT so the app opens straight onto #/bulk-ingest instead of an
    empty library.

    Called both right after confirm_existing_library() creates the account
    (the ordinary first-run path) and from first_run_setup() on every boot
    (the marker can already exist by the time first_run_setup() runs, e.g.
    a restart mid-setup) -- bulk_ingest_run.start_run() is idempotent (returns
    the already-active run rather than starting a second one), and
    _read_trellis_root_marker() returns None on a machine that never chose
    a folder, so this is always safe to call.
    """
    data = _read_trellis_root_marker()
    if not data or not data.get("ingest_existing"):
        return
    from app.utils import bulk_ingest_run
    with app.app_context():
        # Review First: the first run only scans and waits. A person looks at
        # the queue and presses Ingest; nothing is catalogued on its own.
        bulk_ingest_run.start_run(app.config["LIBRARY_ROOT"], "hold")


def _apply_marker(data):
    """Patch config from either marker shape. See _read_trellis_root_marker."""
    if data.get("mode") == "imported":
        _apply_imported_library(
            data["library_root"],
            import_dir   = data.get("import_dir"),
            backlog_dir  = data.get("backlog_dir"),
            workshop_dir = data.get("workshop_dir"),
        )
    else:
        _apply_trellis_root(
            Path(data["trellis_root"]),
            import_dir   = data.get("import_dir"),
            backlog_dir  = data.get("backlog_dir"),
            workshop_dir = data.get("workshop_dir"),
        )


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
        _apply_marker(marker)
        return True

    # Nothing chosen yet: first-run setup runs. A reachable hardcoded
    # default used to count as "configured" (a bypass for the NAS-mounted
    # dev machine); removed 2026-09-27 on Ryan's call. The machine that
    # actually uses /Volumes/music/Trellis has a marker, and a dev checkout
    # must behave like a newcomer's machine so onboarding can be tested.
    return False


def _setup_html():
    """
    The one-time first-run screen. Inline, not a static file -- Flask isn't
    serving anything yet at this point, and this only ever runs once per
    machine. window.location.href navigates this same window over to the
    real app once setup succeeds.

    Redesigned 2026-09-27 (Ryan) from the four-screen wizard down to one
    screen: username and library location are both visible and answered in
    any order, Start Trellis is the only action. File handling is no longer
    asked here at all -- confirm_trellis_root()/confirm_existing_library()
    get null for file_handling_mode/placement. The mode follows the answer
    given: a new library defaults to organize, an existing one to keep
    (Ryan, 2026-10-02); placement falls back to artist. Settings is where
    that gets changed later. Every string
    on this page was approved by Ryan; do not add any without him.
    """
    app_url = f"http://{Config.HOST}:{Config.PORT}"
    home_str = str(Path.home())
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; justify-content: center;
    background: #14161a; color: #e8e6e1;
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
    padding: 64px 20px 40px;
  }}
  .card {{ max-width: 640px; width: 100%; }}
  h1 {{ font-size: 28px; font-weight: 600; margin: 0 0 28px; text-align: center; }}
  .field-block {{ margin-bottom: 24px; text-align: center; }}
  .lbl {{ display: block; font-size: 15px; color: #b8b5ae; margin: 0 0 8px; }}
  input.field {{
    width: 100%; max-width: 240px; font-size: 14px; padding: 9px 12px;
    border-radius: 6px; border: 1px solid #3a3d43; background: #1e2126;
    color: #e8e6e1; font-family: inherit;
  }}
  input.field:focus {{ outline: none; border-color: #d98f4e; }}
  .opt-box {{
    border: 1px solid #3a3d43; border-radius: 6px; margin-bottom: 12px;
  }}
  .opt-box.on {{ border-color: #d98f4e; background: rgba(217,143,78,.12); }}
  .opt {{
    display: flex; gap: 12px; align-items: flex-start; cursor: pointer;
    padding: 16px 18px;
  }}
  .opt input[type=radio] {{ margin: 3px 0 0; accent-color: #d98f4e; flex-shrink: 0; }}
  .opt-head {{ font-size: 16px; font-weight: 600; margin: 0 0 6px; }}
  .opt-body {{ font-size: 13px; color: #b8b5ae; line-height: 1.55; margin: 0; }}
  .opt-detail {{ padding: 0 18px 18px 43px; }}
  /* Folder rows: label, chosen path, select button on one line. */
  .rows {{ border-top: 1px solid #3a3d43; }}
  .row {{
    display: flex; gap: 12px; align-items: center; padding: 10px 0;
    border-bottom: 1px solid #2a2e35; font-size: 14px;
  }}
  .row:last-child {{ border-bottom: none; }}
  .row-label {{ flex: 0 0 150px; }}
  .row-label .opt-hint {{ color: #b8b5ae; }}
  .row .opt-path {{ flex: 1 1 auto; min-width: 0; }}
  .new-pick {{ display: flex; gap: 12px; align-items: center; padding-top: 4px; }}
  .new-pick .opt-path {{ flex: 1 1 auto; min-width: 0; }}
  [hidden] {{ display: none !important; }}
  button.ghost.picked {{ box-shadow: inset 0 0 0 1px #d98f4e; }}
  .opt-path {{
    font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px;
    color: #b8b5ae; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap;
  }}
  button {{
    font-size: 14px; padding: 10px 20px; border-radius: 6px; border: none;
    background: #d98f4e; color: #14161a; font-weight: 600; cursor: pointer;
    font-family: inherit;
  }}
  button.ghost {{ background: #2a2e35; color: #e8e6e1; font-weight: 500; padding: 7px 14px; font-size: 13px; flex-shrink: 0; }}
  button:disabled {{ opacity: .5; cursor: default; }}
  #start-row {{ margin-top: 28px; text-align: center; }}
  #status {{ margin-top: 16px; font-size: 13px; color: #b8b5ae; text-align: center; }}
  #status.err {{ color: #e0806a; }}
</style></head>
<body><div class="card">
  <h1>Welcome to Trellis Music Library</h1>

  <div class="field-block">
    <label class="lbl" for="username">Enter a username</label>
    <input id="username" class="field" type="text" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" autofocus>
  </div>

  <div class="opt-box" id="box-existing">
    <label class="opt">
      <input type="radio" name="kind" value="existing">
      <span>
        <p class="opt-head">I'd like to import an existing library.</p>
        <p class="opt-body">Trellis will first import your full library into a database, without moving or renaming any files. It will infer as much information as it can from directory names, file tags and text files. Any recordings needing attention will be added to a queue. SHN and WAV recordings wait in the queue until you convert them to FLAC or move them to Backlog or Workshop.</p>
      </span>
    </label>
    <div class="opt-detail" id="sub-existing" hidden>
      <div class="rows">
        <div class="row">
          <span class="row-label">Library</span>
          <span class="opt-path" id="path-existing"></span>
          <button class="ghost" id="pick-existing" type="button">Select Location</button>
        </div>
        <div class="row">
          <span class="row-label">Downloads</span>
          <span class="opt-path" id="path-downloads"></span>
          <button class="ghost" id="pick-downloads" type="button">Select Location</button>
        </div>
        <div class="row">
          <span class="row-label">Workshop <span class="opt-hint">(optional)</span></span>
          <span class="opt-path" id="path-workshop"></span>
          <button class="ghost" id="pick-workshop" type="button">Select Location</button>
        </div>
        <div class="row">
          <span class="row-label">Backlog <span class="opt-hint">(optional)</span></span>
          <span class="opt-path" id="path-backlog"></span>
          <button class="ghost" id="pick-backlog" type="button">Select Location</button>
        </div>
      </div>
    </div>
  </div>

  <div class="opt-box" id="box-new">
    <label class="opt">
      <input type="radio" name="kind" value="new">
      <span>
        <p class="opt-head">I'd like to create a new library.</p>
        <p class="opt-body">Trellis will create a "Trellis Music Library" folder, and then a set of useful subfolders within (Library, Downloads, Backlog, and Workshop). You can modify these later. Then you can import your collection (or start a new one) and keep it organized in Trellis.</p>
      </span>
    </label>
    <div class="opt-detail" id="sub-new" hidden>
      <div class="new-pick">
        <span class="opt-path" id="path-new"></span>
        <button class="ghost" id="pick-new" type="button">Select Location</button>
      </div>
    </div>
  </div>

  <div id="start-row"><button id="start" disabled>Start Trellis</button></div>
  <div id="status"></div>
</div>
<script>
  const $ = id => document.getElementById(id)
  const status = $('status')
  const uname  = $('username')
  // Nothing selected until the person picks (Ryan, 2026-10-01).
  let kind = null
  const HOME = {home_str!r}
  // Shown path only: ~ for the home folder. The full path is what is sent.
  function shortPath(p) {{
    if (p === HOME) return '~'
    if (p.startsWith(HOME + '/')) return '~' + p.slice(HOME.length)
    return p
  }}
  function showPath(id, p) {{ const el = $(id); el.textContent = shortPath(p); el.title = p }}
  // Library and Downloads are required for an import; Workshop and Backlog
  // are optional and stay null when skipped.
  const picked = {{ existing: null, downloads: null, workshop: null, backlog: null, new: null }}

  function clearStatus() {{ status.className = ''; status.textContent = '' }}
  function fail(msg) {{ status.className = 'err'; status.textContent = msg }}

  function syncOpts() {{
    $('box-existing').classList.toggle('on', kind === 'existing')
    $('box-new').classList.toggle('on', kind === 'new')
    $('sub-existing').hidden = kind !== 'existing'
    $('sub-new').hidden = kind !== 'new'
  }}
  syncOpts()

  function refreshStart() {{
    const folderResolved = kind === 'existing' ? !!(picked.existing && picked.downloads)
                         : kind === 'new' ? !!picked.new : false
    $('start').disabled = !uname.value.trim() || !folderResolved
  }}

  function choose(k) {{
    document.querySelector(`input[name="kind"][value="${{k}}"]`).checked = true
    kind = k
    syncOpts()
    refreshStart()
  }}

  document.querySelectorAll('input[name="kind"]').forEach(r => r.addEventListener('change', () => choose(r.value)))
  uname.addEventListener('input', refreshStart)

  // One native folder dialog path for every row.
  function wirePick(key, kindToSelect) {{
    $('pick-' + key).addEventListener('click', async e => {{
      e.preventDefault()
      const f = await window.pywebview.api.pick_folder()
      if (!f) return
      picked[key] = f
      showPath('path-' + key, f)
      $('pick-' + key).classList.add('picked')
      choose(kindToSelect)
    }})
  }}
  ;['existing', 'downloads', 'workshop', 'backlog'].forEach(k => wirePick(k, 'existing'))
  wirePick('new', 'new')

  $('start').addEventListener('click', async e => {{
    const btn = e.target
    btn.disabled = true
    clearStatus()
    const username = uname.value.trim()
    try {{
      const api = window.pywebview.api
      const result = kind === 'existing'
        ? await api.confirm_existing_library(picked.existing, username, picked.downloads, picked.backlog, picked.workshop, null, null)
        : await api.confirm_trellis_root(picked.new, username, null, null)
      if (result && result.ok) {{ window.location.href = {app_url!r}; return }}
      fail((result && result.error) || 'Something went wrong. Try again.')
    }} catch (err) {{
      fail(String(err))
    }}
    btn.disabled = false
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

    # resolve() follows a db/trellis.db symlink to its target. Creating only
    # the link's own folder left SQLite facing a missing target folder and
    # failing with "unable to open database file" (2026-09-26).
    Config.DB_PATH.resolve().parent.mkdir(parents=True, exist_ok=True)
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
    _maybe_start_bulk_ingest_from_marker()
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

    def confirm_trellis_root(self, parent_path, username=None,
                              file_handling_mode=None, placement=None):
        """
        First-run only. The user picked a parent folder via pick_folder() and
        typed a name for themselves; this creates
        <parent>/Trellis Music Library/{Library,Downloads,Backlog,Workshop},
        remembers the folder choice for next launch, patches the already-
        running app's config -- Flask started before this could possibly be
        known -- creates the owner account under the chosen name, and applies
        the Files/Placement choice from the same screen (spec section 6.1).
        Called from the setup page's JS.

        The account only gets created here on a genuinely empty database --
        first_run_setup() skips its own automatic account for exactly this
        case (its create_default_user flag). Anywhere that already has an
        account, including the "old default happened to be reachable" case
        that skips this page entirely, this leaves it alone rather than
        renaming it out from under someone.

        Returns {"ok": True, "root": "..."} or {"ok": False, "error": "..."}.
        """
        try:
            root = Path(parent_path) / TRELLIS_ROOT_FOLDER_NAME
            for name in TRELLIS_SUBFOLDERS:
                (root / name).mkdir(parents=True, exist_ok=True)
            _write_trellis_root_marker(root)
            _apply_trellis_root(root)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        try:
            self._create_owner_account(username)
            self._apply_file_handling_choice(file_handling_mode or "organize", placement)
        except Exception as e:
            return {"ok": False, "error": f"Folder created, but account setup failed: {e}"}

        return {"ok": True, "root": str(root)}

    def confirm_existing_library(self, library_path, username=None,
                                 import_dir=None, backlog_dir=None,
                                 workshop_dir=None, file_handling_mode=None,
                                 placement=None):
        """
        First-run, the OTHER answer (2026-09-17): the user already has a
        library and wants Trellis to use it where it sits.

        Nothing is created and nothing is moved. The folder they picked
        becomes LIBRARY_ROOT as-is. The three working folders are optional
        and independent -- a collector may have a Download folder and no
        Backlog, and Trellis does not invent either.

        Refuses a folder it cannot list, because the alternative is an app
        that opens onto an empty library and looks like a fresh install --
        "new install" and "your library moved" looking identical is a trap
        this codebase has already paid for once.
        """
        try:
            root = Path(library_path)
            if not root.is_dir() or not _looks_reachable(root):
                return {"ok": False,
                        "error": "That folder could not be opened. Pick the "
                                 "folder your recordings are in."}
            # Downloads must never overlap the library (same rule as Settings).
            if import_dir and _paths_overlap(os.path.realpath(str(import_dir)),
                                             os.path.realpath(str(root))):
                return {"ok": False, "error": "That folder overlaps your library."}
            _write_marker({
                "mode":         "imported",
                "library_root": str(root),
                "import_dir":   str(import_dir)   if import_dir   else None,
                "backlog_dir":  str(backlog_dir)  if backlog_dir  else None,
                "workshop_dir": str(workshop_dir) if workshop_dir else None,
                # Bulk Ingest (spec 1.9/4, chunk 7): "Use a folder I
                # already have" means there is existing material to walk and
                # turn into Recordings, not an empty library to just open on.
                "ingest_existing": True,
            })
            _apply_imported_library(root, import_dir, backlog_dir, workshop_dir)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        try:
            self._create_owner_account(username)
            self._apply_file_handling_choice(file_handling_mode or "keep", placement)
            _maybe_start_bulk_ingest_from_marker()
        except Exception as e:
            return {"ok": False, "error": f"Library set, but account setup failed: {e}"}

        return {"ok": True, "root": str(root)}

    # The optional working folders of an ingested library (2026-09-26). They
    # were first-run only; first run stopped asking (a newcomer does not know
    # what a Backlog is), so Settings sets them instead. Only an "imported"
    # library has them to set: a created library's working folders are its
    # own fixed subfolders.
    _WORKING_FOLDER_KEYS = ("import_dir", "backlog_dir", "workshop_dir")

    def get_working_folders(self):
        data = _read_trellis_root_marker() or {}
        # Editable for every library (Ryan, 2026-10-01), not just imported.
        out = {"editable": bool(data)}
        triage = app.config.get("TRIAGE_DIRS") or {}
        effective = {"import_dir":   app.config.get("IMPORT_DIR"),
                     "backlog_dir":  triage.get("backlog"),
                     "workshop_dir": triage.get("workshop")}
        for k in self._WORKING_FOLDER_KEYS:
            out[k] = data.get(k) or effective[k]
        # The folder downloads actually land in, set or not (2026-10-01), so
        # Settings can show ~/Downloads/Trellis rather than a blank.
        out["effective_import_dir"] = app.config.get("IMPORT_DIR")
        return out

    def set_working_folder(self, which, path):
        """Store one working folder in the marker and apply it to the running
        app, exactly as first run did. Returns {"ok": ...}."""
        data = _read_trellis_root_marker()
        if not data:
            return {"ok": False, "error": "Not available for this library."}
        if which not in self._WORKING_FOLDER_KEYS:
            return {"ok": False, "error": "Unknown folder."}
        if not path or not Path(path).is_dir():
            return {"ok": False, "error": "That folder could not be opened."}
        if which == "import_dir":
            # Downloads must never overlap the library or a working folder
            # (2026-10-01): Ingest and Move act on its folders as loose material.
            real = os.path.realpath(path)
            lib = os.path.realpath(app.config["LIBRARY_ROOT"])
            if _paths_overlap(real, lib):
                return {"ok": False, "error": "That folder overlaps your library."}
            for k in ("backlog_dir", "workshop_dir"):
                if data.get(k) and os.path.realpath(data[k]) == real:
                    return {"ok": False, "error": "That folder is already used for something else."}
        data[which] = str(path)
        try:
            _write_marker(data)
            _apply_marker(data)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "path": str(path)}

    def _apply_file_handling_choice(self, file_handling_mode, placement):
        """
        Persists the Files/Placement radios from the setup page (mockup panel
        1) via the same node_settings functions Settings itself uses. Both
        params fall back to the safe default (keep / artist) rather than
        raising when the setup page sends something unrecognized, since a
        malformed first-run choice must not be the thing that fails the whole
        setup after the folders and account already succeeded.
        """
        from app.utils import node_settings

        with app.app_context():
            node_settings.apply_mode(
                file_handling_mode if file_handling_mode in ("keep", "organize") else "keep")
            node_settings.set_file_handling(
                placement=placement if placement in ("artist", "root") else "artist")

    def _create_owner_account(self, username):
        """
        Create the owner account under `username` -- same random-password,
        thrown-away-on-the-spot, auto-login design as first_run_setup(); see
        that docstring for why nobody ever types a password on this machine.

        If an account already exists this ADOPTS the typed name onto it
        rather than leaving it alone (Ryan, 2026-08-28). It used to return
        silently, which made this page ask a question and then discard the
        answer: Ryan typed "jeff" on a second install and got `ryanfbaker`,
        his macOS account name, because the database had survived.
        **Deleting the Trellis library folder does not reset an install** --
        the account lives in Application Support, so every "fresh" install on
        a machine that has run Trellis before inherits the first account it
        ever made, and first_run_setup() returns at
        `if Config.DB_PATH.exists()` long before it could make a second one.

        Renaming is safe. `username` is read in exactly three places -- the
        login lookup, the /api/auth/me payload, and User.name's display
        fallback -- and no peer, invite or device row stores it. Flask-Login
        keys the session on the row id, so nobody is signed out by this.

        A retried submission (folders succeeded, this failed the first time)
        is still safe: adopting the same name twice is a no-op.
        """
        import secrets
        import bcrypt
        from app.extensions import db
        from app.models.user import User

        with app.app_context():
            who      = (username or "").strip()[:64]
            existing = db.session.query(User).first()

            if existing is not None:
                # An empty field must never blank out a working name -- only a
                # name the person actually typed gets adopted.
                if who and who != existing.username:
                    clash = (db.session.query(User)
                             .filter(User.username == who, User.id != existing.id)
                             .first())
                    if clash is not None:
                        # Raise rather than skip: confirm_trellis_root() turns
                        # this into a visible message on the setup page. A
                        # second silent fallback is the whole bug being fixed.
                        raise ValueError(
                            f"Another account on this machine is already called {who!r}.")
                    existing.username = who
                    db.session.commit()
                return

            db.session.add(User(
                username      = who or "owner",
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

    # Config is final and the schema exists: only now is it safe to put a
    # download left active at quit back in the queue and restart the worker.
    from app.utils.download_queue import resume_on_boot as _resume_downloads
    _resume_downloads(app)

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
