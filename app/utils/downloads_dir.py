"""
app/utils/downloads_dir.py -- where downloaded and incoming material lands,
and the one place that creates it.

2026-10-01 (Archive Downloads). run.py resolves IMPORT_DIR at boot; for an
imported library with no Downloads folder set it is now ~/Downloads/Trellis.
That folder is NOT created at boot: a macOS permission prompt for ~/Downloads
at launch, with no context, is a bad first impression. It is created here, the
first time something actually needs it (a queued download, the Downloads page,
Add Recordings opening its picker).
"""

import os

from flask import current_app


class DownloadsDirError(Exception):
    """The Downloads folder could not be created or is not reachable."""


def default_downloads_dir():
    """~/Downloads/Trellis, expanded at call time (HOME is patchable in tests)."""
    return os.path.expanduser(os.path.join("~", "Downloads", "Trellis"))


def downloads_dir():
    """The effective Downloads folder, without creating it."""
    return current_app.config.get("IMPORT_DIR") or default_downloads_dir()


def ensure_downloads_dir():
    """
    Return the Downloads folder path, creating it if missing.

    Under the user's home, parents are created too (~/Downloads may not exist).
    Anywhere else only the leaf is created: an unmounted /Volumes/... share
    must fail, not grow a stray directory tree on the boot volume.
    Raises DownloadsDirError.
    """
    path = downloads_dir()
    try:
        if os.path.isdir(path):
            return path
        home = os.path.realpath(os.path.expanduser("~"))
        if os.path.realpath(path).startswith(home + os.sep):
            os.makedirs(path, exist_ok=True)
        else:
            os.mkdir(path)
    except OSError as e:
        raise DownloadsDirError(str(e)) from e
    return path
