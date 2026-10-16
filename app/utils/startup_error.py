"""utils/startup_error.py -- tell the person when Trellis could not start.

The packaged app has no console, so an exception during startup used to look
like the app simply not opening. This shows a native alert (no pywebview, no
Flask, no window needed) and saves the traceback to a log file.

Nothing here may raise: it runs while the app is already failing.
"""

import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

TITLE = "Trellis Music Library"
LOG_NAME = "startup-error.log"
GENERIC_MESSAGE = "Trellis could not start. Details were saved to {log}."
ENV_SHOW = "TRELLIS_SHOW_STARTUP_ERRORS"

_APPLESCRIPT = (
    "on run argv",
    'display alert "' + TITLE + '" message (item 1 of argv) as critical buttons {"OK"}',
    "end run",
)


def data_dir():
    """Same answer as config.Config.DATA_DIR, without needing the app."""
    try:
        override = os.environ.get("TRELLIS_DATA_DIR")
        if override:
            return Path(override)
        from config import Config
        return Path(Config.DATA_DIR)
    except Exception:
        pass
    try:
        home = Path.home()
        if not getattr(sys, "frozen", False):
            return Path(__file__).resolve().parents[2]
        if sys.platform == "darwin":
            return home / "Library" / "Application Support" / TITLE
        if os.name == "nt":
            return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / TITLE
        return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "trellis-music-library"
    except Exception:
        return Path(".")


def log_path():
    return data_dir() / LOG_NAME


def write_log(exc):
    """Write the traceback (overwriting the last one). Returns the path or None."""
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        path.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n\n" + text, encoding="utf-8")
        return path
    except Exception:
        return None


def is_upgrade_error(exc):
    """DatabaseUpgradeError or a subclass, matched by name so db_upgrade need not import."""
    return any(c.__name__ == "DatabaseUpgradeError" for c in type(exc).__mro__)


def message_for(exc, path):
    if is_upgrade_error(exc) and str(exc).strip():
        return str(exc)
    return GENERIC_MESSAGE.format(log=path if path else "the Trellis data folder")


def show_startup_error(message):
    """Native modal alert. Falls back to stderr. Never raises."""
    try:
        if sys.platform == "darwin":
            args = ["osascript"]
            for line in _APPLESCRIPT:
                args += ["-e", line]
            # The message travels as an argument to the script, so it needs no
            # quoting or escaping of any kind.
            args.append(str(message))
            subprocess.run(args, check=True, capture_output=True)
            return
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(message), TITLE, 0x10)
            return
    except Exception:
        pass
    try:
        print(f"{TITLE}: {message}", file=sys.stderr)
    except Exception:
        pass


def should_show():
    return bool(getattr(sys, "frozen", False)) or os.environ.get(ENV_SHOW) == "1"


def handle_startup_failure(exc):
    """Log, tell the person (packaged app, or opted in from source). Never raises."""
    try:
        path = write_log(exc)
        if not getattr(sys, "frozen", False):
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        if should_show():
            show_startup_error(message_for(exc, path))
    except Exception:
        pass


def install_excepthook():
    """Route an uncaught startup exception through handle_startup_failure."""
    default = sys.excepthook

    def hook(exc_type, exc, tb):
        if exc is not None and exc.__traceback__ is None:
            exc = exc.with_traceback(tb)
        handle_startup_failure(exc)
        # The default hook prints the traceback; the interpreter then exits 1.
        default(exc_type, exc, tb)

    sys.excepthook = hook
