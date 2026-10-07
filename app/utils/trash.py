"""
app/utils/trash.py -- move a folder to the system Trash (Ryan, 2026-10-06).

The working-folder pages still delete nothing outright: Move to Trash hands the
folder to the operating system's Trash, where Finder can put it back. macOS
only, through Foundation's NSFileManager (pyobjc ships with pywebview). A
volume with no Trash makes NSFileManager refuse rather than delete, and that
refusal is passed on as the error.
"""


class TrashUnavailable(RuntimeError):
    pass


def move_to_trash(path):
    try:
        from Foundation import NSFileManager, NSURL
    except ImportError as e:
        raise TrashUnavailable("Move to Trash is not available on this computer.") from e
    url = NSURL.fileURLWithPath_(path)
    ok, _new_url, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(url, None, None)
    if not ok:
        raise OSError(str(err.localizedDescription()) if err is not None else "Could not move to Trash.")
