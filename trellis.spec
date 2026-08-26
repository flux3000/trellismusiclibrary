# -*- mode: python ; coding: utf-8 -*-
"""
trellis.spec — PyInstaller recipe for the double-clickable Trellis app.

    python3 -m PyInstaller trellis.spec --noconfirm

Build on the platform you are targeting: PyInstaller does not cross-compile, so
a Mac app must be built on a Mac. See tools/build_macos.sh.

Everything below is here because leaving it out produces an app that launches
and then dies, usually with a message that names something unrelated.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_submodules, collect_data_files, collect_dynamic_libs,
)

sys.path.insert(0, str(Path(SPECPATH)))
from version import __version__, APP_NAME, SHORT_NAME   # noqa: E402

# ── Files the app reads at runtime ───────────────────────────────────────────
# The frontend and the self-hosted fonts. config.resource_dir() resolves these
# to PyInstaller's unpack directory at runtime, which is why they are placed at
# the same relative path they occupy in the repo.
datas = [("app/static", "app/static")]

# soundfile ships its OWN copy of libsndfile, loaded through ctypes rather than
# imported — PyInstaller follows Python imports and cannot see it. Without it
# the app dies on first launch complaining about a library nobody has heard of.
#
# The package name is the trap. `soundfile` is a single MODULE, so asking the
# collectors about it returns NOTHING, silently — the first version of this spec
# did exactly that and would have shipped a bundle that crashed on Jim's Mac.
# The library actually lives in a separate top-level package, `_soundfile_data`.
#
# PyInstaller does ship hook-soundfile.py which handles this, so on a current
# version these lines are belt and braces. They stay because a hook that quietly
# stops applying is indistinguishable from one that never ran, and this failure
# only shows up on someone else's computer.
# geonamescache ships its city/country tables as JSON beside the code and has
# no PyInstaller hook. app/utils/ingest.py builds its lookup sets AT IMPORT
# TIME, so a bundle without these files does not fail later during an ingest —
# it fails on launch, before the window appears, with a FileNotFoundError
# naming a path nobody recognises. (Exactly what happened, 2026-08-25.)
#
# numpy, scipy, matplotlib and sqlalchemy are NOT listed here on purpose:
# PyInstaller ships hooks for all four, and collecting them by hand would add
# hundreds of megabytes of headers and test data for no benefit.
_geonames = collect_data_files("geonamescache")
if not _geonames:
    raise SystemExit(
        "Refusing to build: geonamescache's data files were not found.\n"
        "Without them the app dies on launch, before it can say why."
    )
datas   += _geonames

datas   += collect_data_files("_soundfile_data")
binaries = collect_dynamic_libs("_soundfile_data")
if not binaries:
    raise SystemExit(
        "Refusing to build: libsndfile was not found in _soundfile_data.\n"
        "soundfile's packaging has changed. Locate the .dylib/.so it loads and "
        "add it here — a build without it launches and dies on the target "
        "machine, which is the worst place to discover it."
    )

# ── Imports nothing can see by reading the source ────────────────────────────
hiddenimports = [
    # run.py registers every model with importlib.import_module("app.models"),
    # and the models import each other lazily. A static scan misses most of it,
    # and a missing model is a missing TABLE on first run.
    *collect_submodules("app"),

    # keyring picks its backend at runtime by platform. Both are named so one
    # spec serves both platforms; the wrong one simply never loads.
    "keyring.backends.macOS",
    "keyring.backends.Windows",
    "keyring.backends.chainer",
    "keyring.backends.fail",

    # pywebview also chooses its GUI backend at runtime.
    "webview.platforms.cocoa",
    "webview.platforms.winforms",

    "flask_sqlalchemy",
    "sqlalchemy.dialects.sqlite",
    "bcrypt",
    "waitress",
    "pyloudnorm",
    "geonamescache",
]

excludes = [
    "tkinter",          # nothing here uses it and it drags in a whole toolkit
    "pytest",
    "PyInstaller",
]

a = Analysis(
    ["run.py"],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    strip=False,
    upx=False,          # UPX and macOS code signing do not get along
    console=False,      # no terminal window behind the app
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name=APP_NAME,
)

# BUNDLE is a no-op off macOS, so this spec stays valid on any platform.
app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon="assets/icon/Trellis.icns",
    bundle_identifier="com.trellismusiclibrary.trellis",
    version=__version__,
    info_plist={
        # Menu bar — short, or macOS truncates it.
        "CFBundleName":             SHORT_NAME,
        # Finder, About, everywhere with room for the real name.
        "CFBundleDisplayName":      APP_NAME,
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion":          __version__,
        # Without this the whole window renders at 1x and looks blurry on any
        # modern display — the single most common "why does it look wrong"
        # report for a packaged PyWebView app.
        "NSHighResolutionCapable":  True,
        "LSMinimumSystemVersion":   "11.0",
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHumanReadableCopyright": "Trellis Music Library",
    },
)
