"""app/utils/published.py -- a recording in a working folder is not published.

Recording.location is the source of truth; is_published is derived (published
exactly when location == 'library') and decides what the library lists show and
what Full Library shares. Ryan, 2026-10-15. Move sets the location; this covers a
recording that was catalogued where it sits inside a working folder (Backlog and
Workshop may live inside the library).

Path arithmetic only, no disk access: it runs over every row at boot, against
a NAS. Nothing here ever sets 'library': that is Import.
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


def working_folder_name(abs_path):
    """'workshop' / 'backlog' / 'downloads' for a path inside one, else None."""
    from app.utils.downloads_dir import downloads_dir
    named = [("downloads", downloads_dir()),
             *((current_app.config.get("TRIAGE_DIRS") or {}).items())]
    p = _norm(abs_path)
    for name, d in named:
        if d and (p == _norm(d) or p.startswith(_norm(d) + os.sep)):
            return name
    return None


def in_working_folder(abs_path, folders=None):
    p = _norm(abs_path)
    return any(p == w or p.startswith(w + os.sep) for w in (folders or working_folders()))


def is_published_location(folder_path):
    """False for a library-relative folder_path inside a working folder."""
    root = current_app.config.get("LIBRARY_ROOT") or ""
    return not in_working_folder(os.path.join(str(root), folder_path or ""))


# How many rows the last boot pass could not place (see below); read by tests and the log line.
LAST_UNMAPPED = 0


def unpublish_working_folder_recordings():
    """Boot pass: put every recording that is catalogued in place inside a working
    folder where it actually is. Location is the source of truth (published exactly
    when location == 'library'), so a row whose folder sits in the Workshop or Backlog
    folder (which may live under the library root) gets that location and a folder_path
    relative to that working folder. Returns how many rows changed. Commits.

    Rows it cannot place stay in the library and are counted in LAST_UNMAPPED and
    logged: a folder inside Downloads (not a location), or a row the old is_published
    column calls unpublished whose folder is in no configured working folder.
    Path arithmetic only, no disk access. Idempotent; never sets 'library'."""
    global LAST_UNMAPPED
    from app.extensions import db
    from app.models.recording import Recording
    root = str(current_app.config.get("LIBRARY_ROOT") or "")
    triage = {k: _norm(v) for k, v in (current_app.config.get("TRIAGE_DIRS") or {}).items() if v}
    folders = working_folders()
    changed, unmapped = 0, 0
    rows = (db.session.query(Recording.id, Recording.folder_path, Recording._is_published_legacy)
            .filter(Recording.location == "library").all())
    for rid, fp, legacy_pub in rows:
        if not fp:
            continue
        absf = _norm(os.path.join(root, fp))
        dest = next((name for name, d in triage.items()
                     if absf == d or absf.startswith(d + os.sep)), None)
        if dest in ("workshop", "backlog") and absf != triage[dest]:
            rel = os.path.relpath(absf, triage[dest])
            (db.session.query(Recording).filter(Recording.id == rid)
             .update({"location": dest, "folder_path": rel, "_is_published_legacy": False},
                     synchronize_session=False))
            changed += 1
        elif in_working_folder(absf, folders) or not legacy_pub:
            unmapped += 1
    # Queue rows an earlier build held ("Already in Workshop or Backlog") are read again: the
    # folder now links to its recording, or skips as the duplicate it is.
    from app.models.bulk_ingest import BulkIngestItem
    held = (db.session.query(BulkIngestItem)
            .filter(BulkIngestItem.status == "review", BulkIngestItem.reason.like("%in_working_folder%"))
            .update({"status": "pending", "reason": None, "duplicate_of": None, "detail": None},
                    synchronize_session=False))
    if changed or held:
        db.session.commit()
    LAST_UNMAPPED = unmapped
    if unmapped:
        current_app.logger.warning(
            "%d recording(s) are marked off the shelf but sit in no Workshop or Backlog folder; "
            "left in the library.", unmapped)
    return changed


def backfill_working_folder_locations():
    """Rows moved out of the library before Recording.location existed still say
    location 'library' with the old library-relative folder_path. For each
    unpublished one, read its LATEST moved_out_of_library event ("Moved to
    <destination> as <name>"); when <working folder>/<name> is a directory,
    point the row at it. A folder that is not found leaves the row as it is.
    Idempotent, stat calls only, never fatal. Returns how many rows changed.
    Commits."""
    import re
    from app.extensions import db
    from app.models.recording import Recording
    from app.models.recording_event import RecordingEvent
    triage = current_app.config.get("TRIAGE_DIRS") or {}
    changed = 0
    try:
        rows = (db.session.query(Recording)
                .filter(Recording._is_published_legacy.is_(False), Recording.location == "library")
                .all())
        for rec in rows:
            ev = (db.session.query(RecordingEvent)
                  .filter(RecordingEvent.recording_id == rec.id,
                          RecordingEvent.event_type == "moved_out_of_library")
                  .order_by(RecordingEvent.id.desc()).first())
            m = re.match(r"^Moved to (\w+) as (.+)$", (ev.note or "") if ev else "", re.S)
            if not m:
                continue
            dest, name = m.group(1).lower(), m.group(2)
            root = triage.get(dest)
            if (not root or dest not in ("workshop", "backlog")
                    or os.sep in name or name in (".", "..")
                    or not os.path.isdir(os.path.join(str(root), name))):
                continue
            rec.location, rec.folder_path = dest, name
            changed += 1
        if changed:
            db.session.commit()
    except Exception:                                        # noqa: BLE001
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return 0
    return changed


def list_all_requested(args):
    """True when an entity list endpoint should NOT hide entities: a form's
    autocomplete (`q`) and the editing screens that ask for `all=1`. The index pages
    and navigation lists (neither) hide an entity only when it has recordings and
    every one of them is outside the library (Queue, Workshop, Backlog); see
    entity_listed (Ryan, 2026-10-15)."""
    return bool(args.get("all") or (args.get("q") or "").strip())


def entity_listed(lib_count, total_count, show_all=False):
    """The one index/nav list rule. Show an entity when show_all, or it has a library
    recording, or it has no recordings at all (made by hand). Hidden only when it has
    recordings and none are in the library. total_count counts recordings in any
    location, through the same join path as lib_count."""
    return bool(show_all or (lib_count or 0) > 0 or not (total_count or 0))
