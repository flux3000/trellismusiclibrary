"""app/utils/album_completeness.py -- is an imported Album part of a longer release?

Ryan, 2026-10-13. Two checks, cheapest first:
  1. the files' own tags: TRACKTOTAL / DISCTOTAL against the files present
     (resolve.tag_track_shortfall, the same rule the Queue uses);
  2. MusicBrainz: the linked release's track count when there is one, else
     find_incomplete_release() (a same-titled release holding every track).
An incomplete Album gets is_complete = False (the "inc" mark). This never sets
it True: a person may have marked it incomplete for a reason of their own.
completeness_checked_at is stamped once the question was answered; when
MusicBrainz could not be asked it stays NULL and the follow-up tries again.
"""
import logging
import os
from datetime import datetime, timezone

from flask import current_app

log = logging.getLogger(__name__)


def _tag_scan(rec, tracks):
    """A scan-shaped dict of the tracks' tags, or None when the files are not
    there to read (moved out, unmounted)."""
    from app.utils.ingest import open_tags
    from app.utils.paths import recording_folder_abs
    root = recording_folder_abs(rec)
    files, tag_tracks = [], []
    for t in tracks:
        path = os.path.join(root, t.file_path or "")
        if not os.path.isfile(path):
            return None
        try:
            tags = open_tags(path)
            raw = {k.lower(): (v[0] if isinstance(v, list) and len(v) == 1 else v)
                   for k, v in tags.items()} if tags is not None else None
        except Exception:                                    # noqa: BLE001
            raw = None
        files.append({"filename": t.file_path, "rel_path": t.file_path,
                      "disc_number": getattr(t, "disc_number", None)})
        tag_tracks.append({"filename": t.file_path, "rel_path": t.file_path, "raw": raw})
    return {"audio_files": files, "suggestions": {"from_tags": {"tracks": tag_tracks}}}


def check_album(rec):
    """True (incomplete), False (no sign of it), None (could not tell yet).
    Does not commit."""
    from app.utils import musicbrainz as mb
    from app.utils.resolve import tag_track_shortfall
    if rec.kind != "studio":
        return None
    tracks = sorted(rec.tracks or [], key=lambda t: t.track_number or 0)
    if not tracks:
        return None

    scan = _tag_scan(rec, tracks)
    incomplete = bool(scan and tag_track_shortfall(scan))
    if not incomplete:
        if not mb.enabled() or mb.tripped():
            return None
        if rec.mb_release_id:
            rel = mb.lookup_release(rec.mb_release_id)
            if rel is None:
                return None
            incomplete = len(rel.get("tracks") or []) > len(tracks)
        else:
            artist = rec.performance.artist.name if rec.performance and rec.performance.artist else None
            found = mb.find_incomplete_release(artist, rec.title, [t.title for t in tracks])
            if found is None and mb.tripped():
                return None
            incomplete = bool(found)

    if incomplete:
        rec.is_complete = False
    rec.completeness_checked_at = datetime.now(timezone.utc)
    return incomplete
