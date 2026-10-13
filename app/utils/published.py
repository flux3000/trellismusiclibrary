"""app/utils/published.py -- a recording in a working folder is not published.

Recording.is_published decides what the library lists show (Recently Added,
Live Recordings, Albums, the Top Shelf) and what Full Library shares. Ryan,
2026-10-13: anything in Backlog, Workshop or Downloads is unpublished. Move
already sets it; this covers a recording that was catalogued where it sits
inside a working folder (Backlog and Workshop may live inside the library).

Path arithmetic only, no disk access: it runs over every row at boot, against
a NAS. Never sets True: a moved-out recording keeps its old library-relative
folder_path, so its location here proves nothing about where it is now.
"""
import os
import unicodedata

from flask import current_app


def on_shelf(recordings):
    """The published ones, order kept. Every library list goes through this or
    filters Recording.is_published in SQL."""
    return [r for r in recordings or [] if r.is_published]


def _norm(path):
    return unicodedata.normalize("NFC", os.path.normpath(str(path))).rstrip(os.sep)


def working_folders():
    """Downloads, Backlog and Workshop as configured right now."""
    from app.utils.downloads_dir import downloads_dir
    dirs = [downloads_dir(), *(current_app.config.get("TRIAGE_DIRS") or {}).values()]
    return [_norm(d) for d in dirs if d]


def in_working_folder(abs_path, folders=None):
    p = _norm(abs_path)
    return any(p == w or p.startswith(w + os.sep) for w in (folders or working_folders()))


def is_published_location(folder_path):
    """False for a library-relative folder_path inside a working folder."""
    root = current_app.config.get("LIBRARY_ROOT") or ""
    return not in_working_folder(os.path.join(str(root), folder_path or ""))


def unpublish_working_folder_recordings():
    """Set is_published False on every published recording whose folder is in
    a working folder. Returns how many changed. Commits."""
    from app.extensions import db
    from app.models.recording import Recording
    root = str(current_app.config.get("LIBRARY_ROOT") or "")
    folders = working_folders()
    ids = [rid for rid, fp in db.session.query(Recording.id, Recording.folder_path)
           .filter(Recording.is_published.is_(True)).all()
           if fp and in_working_folder(os.path.join(root, fp), folders)]
    if ids:
        (db.session.query(Recording).filter(Recording.id.in_(ids))
         .update({"is_published": False}, synchronize_session=False))
        db.session.commit()
    return len(ids)
