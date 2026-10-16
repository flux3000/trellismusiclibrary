"""Engineer credits for recordings already in the library (Ryan, 2026-10-13).

Reads, per live recording, the same evidence the resolver uses at import:
  1. the files' tags (ENGINEER / TIPL, TAPER, TRANSFER ...), then COMMENT
  2. the stored info file (Recording.info_file_content)
and fills only the roles that are still empty. A name a person already set is
never replaced. Dry run by default; scripts/backfill_engineers.py is the
command-line entry point.
"""
import os

from flask import current_app

from app.extensions import db
from app.utils.engineers import ROLES, extract_engineers, engineers_from_tags, apply_engineer_names


def _tag_evidence(rec, library_root, max_files=3):
    """(roles from ENGINEER-style tags, roles from COMMENT) from the first few
    audio files. Unreadable or missing files give nothing."""
    from app.utils.ingest import open_tags
    tag_roles, comments = {}, []
    from app.utils.paths import recording_folder_abs
    folder = recording_folder_abs(rec, library_root)
    if not library_root or not folder:
        return tag_roles, {}
    for t in list(rec.tracks)[:max_files]:
        path = os.path.join(folder, t.file_path or "")
        try:
            audio = open_tags(path)
        except Exception:  # noqa: BLE001 - a bad file is just no evidence
            audio = None
        if audio is None:
            continue
        for k, v in engineers_from_tags(audio).items():
            tag_roles.setdefault(k, v)
        for c in (audio.get("COMMENT") or []):
            if c and str(c) not in comments:
                comments.append(str(c))
    comment = {f"{r}_engineer": v for r, v in extract_engineers("\n".join(comments)).items()}
    return tag_roles, comment


def backfill_engineers(apply=False, read_tags=True, limit=None):
    """Returns a list of rows {id, folder, role, name, source} for every credit
    found for an empty role. With apply=True the credits are saved."""
    from app.models.recording import Recording
    library_root = current_app.config.get("LIBRARY_ROOT", "") if read_tags else ""
    q = (db.session.query(Recording).filter(Recording.kind != "studio")
         .order_by(Recording.id))
    if limit:
        q = q.limit(limit)
    rows = []
    for rec in q:
        empty = [f"{r}_engineer" for r in ROLES if getattr(rec, f"{r}_engineer_id") is None]
        if not empty:
            continue
        tags, comment = _tag_evidence(rec, library_root) if read_tags else ({}, {})
        info = {f"{r}_engineer": v for r, v in extract_engineers(rec.info_file_content or "").items()}
        found = {}
        for key in empty:
            for source, d in (("tags", tags), ("info", info), ("comment", comment)):
                if d.get(key):
                    found[key] = d[key]
                    rows.append({"id": rec.id, "folder": rec.folder_path, "role": key.split("_")[0],
                                 "name": d[key], "source": source})
                    break
        if apply and found:
            apply_engineer_names(rec, found, clear_blank=False)
    if apply:
        db.session.commit()
    else:
        db.session.rollback()
    return rows
