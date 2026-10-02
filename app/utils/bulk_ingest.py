"""
app/utils/bulk_ingest.py -- pure functions shared by Bulk Ingest and the
field resolver (Ingest Field Resolver spec v1).

extract()/classify()/confirm_payload() and the per-field merge rules that
used to live here moved to app/utils/resolve.py (resolve(), classify_kind(),
verdict(), resolve_tracks()) -- this file kept only the pieces that are
still someone else's dependency: folder_format() (batch_scan, Review &
Ingest), and the folder-tree-artist walk / tag-consistency helpers
resolve.py's own artist and album rules call.

_tag_container_values()/_consistent_tag() and _resolve_venue() moved on to
app/utils/resolve.py too (2026-09-28) -- resolve() was their only real
caller left, so resolve.py is their single home now.

No network: this module never imports musicbrainz or anything that reaches
the internet (tests/test_bulk_ingest_classify.py asserts this directly).
"""
import re
from pathlib import Path

from app.utils.ingest import FOLDER_DATE_RE

# scan_folder() puts every recognised-but-unreadable audio format (SHN,
# APE, ...) in "unsupported_audio", but .wav is deliberately in
# AUDIO_EXTENSIONS (it's a recognised container) even though open_tags()
# can't read tags from one. Only these two formats yield readable tag data.
_READABLE_AUDIO_EXTS = {".flac", ".mp3"}

# Labels for the log's Format column (2026-09-27 progress/log redesign):
# every readable AUDIO_EXTENSIONS format, plus SHN specifically -- the one
# recognised-but-unreadable format worth naming (scan_folder() puts SHN in
# "unsupported_audio" alongside APE etc., but only SHN is common enough here
# to call out by name).
_FORMAT_LABELS = {".flac": "FLAC", ".mp3": "MP3", ".wav": "WAV", ".shn": "SHN"}


def folder_format(scan):
    """Comma-joined, alphabetical format labels found in one scan_folder()
    result -- "FLAC", "WAV", or "FLAC, MP3" for a mixed folder. None when
    nothing recognisable was found at all.

    Public (no leading underscore) as of 2026-09-27: batch_scan() and the
    Review & Ingest staging payload (app/api/quality.py's _scan_metadata)
    call this directly so all three ingest queue tables show the same
    FORMAT pill for the same folder."""
    labels = set()
    for f in scan.get("audio_files", []):
        ext = Path(f["filename"]).suffix.lower()
        if ext in _FORMAT_LABELS:
            labels.add(_FORMAT_LABELS[ext])
    for f in scan.get("unsupported_audio", []):
        ext = Path(f["filename"]).suffix.lower()
        if ext == ".shn":
            labels.add(_FORMAT_LABELS[ext])
    return ", ".join(sorted(labels)) if labels else None


def _bare_year(name):
    return bool(re.match(r'^\d{4}$', name))


def _date_like(name):
    if FOLDER_DATE_RE.search(name):
        return True
    return bool(re.match(r'^\d{4}[-._]\d{2}[-._]\d{2}$', name))


def _folder_tree_artist(folder_abs, library_root):
    """
    Walk from the show folder's parent upward, stopping below library_root.
    The first ancestor whose name is not a bare year, not date-like, and
    does not start with "_" or "." is the artist candidate. None if the
    walk reaches library_root (or leaves it) with no candidate.
    """
    folder = Path(folder_abs).resolve()
    lib = Path(library_root).resolve()
    cur = folder.parent
    while cur != lib and lib in cur.parents:
        name = cur.name
        if not _bare_year(name) and not _date_like(name) \
                and not name.startswith("_") and not name.startswith("."):
            return name
        cur = cur.parent
    return None


