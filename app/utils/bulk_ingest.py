"""
app/utils/bulk_ingest.py -- pure functions for Bulk Ingest (spec section 1.3, 1.4, chunk 4).

extract() wraps the existing scan_folder()/build_scan_payload() foundation
for one show folder and flattens it into the fields Bulk Ingest's review
table needs. classify() turns that into a status/reason/kind. Both are pure
(no DB, no network, no filesystem writes) so they can run over a whole
library without a request context.

No network: this module never imports musicbrainz or anything that reaches
the internet (tests/test_bulk_ingest_classify.py asserts this directly).
"""
import re
from pathlib import Path

from app.utils.ingest import build_scan_payload, scan_folder
from app.utils.venues import is_placeholder_venue_name

# Same shape as app/api/ingest.py's _DATE_RE (full YYYY-MM-DD/./_ folder-name
# date). Duplicated rather than imported: app.api is the route layer and
# imports app.utils, so importing the other way round would invert that
# dependency for one regex.
_DATE_RE = re.compile(r'\b(19|20)\d{2}[-._](0[1-9]|1[0-2])[-._](0[1-9]|[12]\d|3[01])\b')

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
    if _DATE_RE.search(name):
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


def _tag_container_values(tracks, *keys):
    """
    Per-track raw tag value for the first key present, out of `keys`
    (e.g. "album", or "artist"/"albumartist").

    A track with NO readable tags at all (raw is empty -- an unreadable/
    corrupt file, or a format like .wav with no tags interface) is left OUT
    of the comparison entirely (N7): consistency is judged across the files
    that actually yielded tags, not poisoned by a sibling that could not be
    read. A track that WAS read but simply lacks this particular tag still
    counts (as None), so a genuine mismatch between two tagged files is
    still caught. Returns None only when nothing in the folder has any tag
    data to compare.
    """
    values = []
    for t in tracks:
        raw = t.get("raw")
        if not raw:
            continue
        v = None
        for k in keys:
            v = raw.get(k)
            if v:
                break
        values.append(v)
    return values or None


def _consistent_tag(tracks, *keys):
    """The tag value shared by EVERY track, or None on any mismatch/absence."""
    values = _tag_container_values(tracks, *keys)
    if not values or not values[0]:
        return None
    return values[0] if all(v == values[0] for v in values) else None


def _resolve_date(payload):
    """Most-precise date, one whole source at a time: tags, then info file,
    then the folder name -- same precedence build_scan_payload's callers use
    today (see app/api/ingest.py's batch-scan preview)."""
    from_tags = payload["suggestions"]["from_tags"]
    from_info = payload["suggestions"]["from_info_file"]
    folder_name = payload.get("folder_name", "")

    concert_date_tag = from_tags.get("concert_date") or ""
    m = re.match(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", concert_date_tag)
    if m and m.group(1):
        year  = int(m.group(1))
        month = int(m.group(2)) if m.group(2) else None
        day   = int(m.group(3)) if m.group(3) else None
        return year, month, day, "tags"

    if from_info.get("year"):
        return from_info.get("year"), from_info.get("month"), from_info.get("day"), "info"

    fm = _DATE_RE.search(folder_name)
    if fm:
        g = fm.group(0)
        return int(g[:4]), int(g[5:7]), int(g[8:10]), "folder"

    return None, None, None, None


def _any_source_has_month(payload):
    """Whether ANY of tags / info file / folder name gives a month -- used
    only to decide studio-vs-live, independent of which single source
    _resolve_date() picked."""
    from_tags = payload["suggestions"]["from_tags"]
    from_info = payload["suggestions"]["from_info_file"]
    folder_name = payload.get("folder_name", "")

    concert_date_tag = from_tags.get("concert_date") or ""
    if re.match(r"\d{4}-\d{2}", concert_date_tag):
        return True
    if from_info.get("month"):
        return True
    if _DATE_RE.search(folder_name):
        return True
    return False


def _resolve_venue(payload):
    """Tags win over info file; a placeholder name ("Unknown Venue", "TBD",
    ...) counts as no venue at all, from either source."""
    from_tags = payload["suggestions"]["from_tags"]
    from_info = payload["suggestions"]["from_info_file"]
    for source, d in (("tags", from_tags), ("info", from_info)):
        v = d.get("venue")
        if v and not is_placeholder_venue_name(v):
            return v, source
    return None, None


def extract(folder_abs, library_root, placement):
    """
    Scan one show folder and flatten build_scan_payload()'s output into the
    fields Bulk Ingest's review table needs. Reuses build_scan_payload()
    for all tag/info-file/folder-name inference; nothing here re-implements
    that extraction.
    """
    result = {
        "artist": None, "artist_source": None,
        "year": None, "month": None, "day": None, "date_source": None,
        "venue": None, "venue_source": None,
        "city": None, "state": None, "country": None,
        "album": None,
        "source": None, "lineage": None, "source_tag": None, "shnid": None,
        "track_count": 0,
        "has_audio": False,
        "unsupported_only": False,
        "all_unreadable": False,
        "unreadable_detail": None,
        "payload": None,
        "format": None,
    }

    scan = scan_folder(folder_abs)
    readable_files = [f for f in scan["audio_files"]
                      if Path(f["filename"]).suffix.lower() in _READABLE_AUDIO_EXTS]
    any_audio = bool(scan["audio_files"]) or bool(scan["unsupported_audio"])
    result["has_audio"] = any_audio
    result["unsupported_only"] = any_audio and not readable_files
    result["format"] = folder_format(scan)

    if not any_audio or not readable_files:
        return result

    payload = build_scan_payload(folder_abs)
    result["payload"] = payload
    if payload is None:
        # Shouldn't happen when readable_files is non-empty, but be defensive.
        return result

    from_tags = payload["suggestions"]["from_tags"]
    from_info = payload["suggestions"]["from_info_file"]

    # S8 (R2-N3): every readable-format file still has to actually OPEN --
    # a folder whose FLAC/MP3 files are all corrupt must not classify like a
    # clean, untagged one (both would otherwise come back with no artist and
    # land in review as needs_artist rather than failing as unreadable).
    # read_flac_tags (inside build_scan_payload, just above) already tried
    # to open every one of these files once; its "raw" is None only when
    # that open failed, so this reuses that result instead of opening each
    # file a second time here.
    readable_tags = [t for t in from_tags["tracks"]
                      if Path(t.get("filename") or "").suffix.lower() in _READABLE_AUDIO_EXTS]
    unopenable_tags = [t for t in readable_tags if t.get("raw") is None]
    if readable_tags and len(unopenable_tags) == len(readable_tags):
        result["all_unreadable"] = True
        result["unreadable_detail"] = next(
            (t.get("open_error") for t in unopenable_tags if t.get("open_error")), None)
        return result

    result["track_count"] = payload["audio_file_count"]

    # -- Artist: tags > info file > folder tree (placement "artist" only) --
    tag_artist = _consistent_tag(from_tags["tracks"], "artist", "albumartist")
    if tag_artist:
        result["artist"], result["artist_source"] = tag_artist, "tags"
    elif from_info.get("artist"):
        result["artist"], result["artist_source"] = from_info["artist"], "info"
    elif placement == "artist":
        folder_artist = _folder_tree_artist(folder_abs, library_root)
        if folder_artist:
            result["artist"], result["artist_source"] = folder_artist, "folder"

    # -- Date --
    result["year"], result["month"], result["day"], result["date_source"] = \
        _resolve_date(payload)

    # -- Venue (never from folder name) + city/state/country --
    result["venue"], result["venue_source"] = _resolve_venue(payload)
    result["city"]    = from_tags.get("city")    or from_info.get("city")
    result["state"]   = from_tags.get("state")   or from_info.get("state")
    result["country"] = from_tags.get("country") or from_info.get("country")

    # -- Album: only when every readable file agrees --
    result["album"] = _consistent_tag(from_tags["tracks"], "album")

    # -- Source / lineage / source_tag / shnid --
    result["source"]     = from_tags.get("source")  or from_info.get("source")
    result["lineage"]    = from_tags.get("lineage")  or from_info.get("lineage")
    result["source_tag"] = from_info.get("source_tag")
    result["shnid"]      = from_info.get("etree_shnid")

    return result


def classify_kind(payload, album, venue):
    """
    "studio" when ALBUM is identical across every readable file, no source
    (tag, info file, or folder name) states a month anywhere, and no venue
    was found; else "live". Factored out of classify() (2026-09-27) so a
    caller that only wants Type -- batch_scan(), the Review & Ingest staging
    payload -- can get it without running classify()'s has_audio/unreadable/
    artist/date checks, none of which bear on kind.
    """
    no_month_anywhere = (not _any_source_has_month(payload)) if payload else True
    return "studio" if (album is not None and no_month_anywhere and venue is None) else "live"


def classify(extracted):
    """
    (status, reason, kind). See bulk_ingest.py module docstring / spec section 1.4
    for the full table. `kind` is computed first (independent of the status
    checks below it) since a studio row skips the date/venue requirements a
    live one needs.
    """
    payload = extracted.get("payload")
    album   = extracted.get("album")
    venue   = extracted.get("venue")

    kind = classify_kind(payload, album, venue)

    if not extracted["has_audio"]:
        return "failed", "no_audio", kind
    if extracted["unsupported_only"]:
        return "review", "unsupported_format", kind
    if extracted.get("all_unreadable"):
        return "failed", "unreadable", kind
    if not extracted.get("artist"):
        return "review", "needs_artist", kind
    if kind == "studio":
        return "ingested", None, "studio"

    year, month, day = extracted["year"], extracted["month"], extracted["day"]
    if year and month and day:
        return "ingested", None, "live"
    if year and venue:
        return "ingested", None, "live"
    if year and month and not day:
        return "ingested", None, "live"
    return "review", "needs_date", "live"


def confirm_payload(extracted, kind):
    """
    Build the body _do_confirm() expects (see app/api/ingest.py confirm()
    ~638 and _do_confirm() ~813). Does NOT call _do_confirm -- chunk 5 does
    that, after deciding how a bulk/auto-ingested job differs from the
    interactive Add Recording flow.

    _do_confirm does not currently read "title" or "kind" from this dict
    (Recording has a `title` column but nothing populates it from `data` yet;
    there is no `kind`/recording-type column at all) -- both are included
    here anyway, as inert extra keys, for chunk 5 to wire in.
    """
    payload = extracted.get("payload") or {}
    from_tags = (payload.get("suggestions") or {}).get("from_tags", {})
    from_info = (payload.get("suggestions") or {}).get("from_info_file", {})

    # disc_number/disc_track_number/set_number by rel_path, straight from the
    # scan's own audio_files list -- the single source of truth for disc/set
    # membership no matter how it was detected (subdir, filename, or a
    # DISCNUMBER tag carrier -- see scan_folder()/_apply_tag_disc_carrier in
    # app/utils/ingest.py).
    audio_files = payload.get("audio_files") or []
    disc_by_rel_path = {}
    for af in audio_files:
        rel = af.get("rel_path") or af.get("filename")
        if rel is not None:
            disc_by_rel_path[rel] = af

    # A recording is multi-disc when ANY file carries a disc_number. This is
    # deliberately not payload["sets_detected"]: _apply_tag_disc_carrier
    # populates disc_number/disc_track_number for a flat, DISCNUMBER-tagged
    # folder but never flips that top-level flag, and app.js's toTracks()
    # gates on the flag alone -- so this check is a stricter, correct
    # superset of what the interactive path currently does.
    multi_disc = any(af.get("disc_number") is not None for af in audio_files)

    tag_tracks  = from_tags.get("tracks", [])
    info_tracks = from_info.get("tracks", [])
    tracks_out = []
    for idx in range(extracted.get("track_count") or 0):
        tag_t  = tag_tracks[idx]  if idx < len(tag_tracks)  else {}
        info_t = info_tracks[idx] if idx < len(info_tracks) else {}
        tag_title  = (tag_t.get("title") or "").strip()
        info_title = (info_t.get("title") or "").strip()

        rel_path   = tag_t.get("rel_path") or tag_t.get("filename")
        disc       = disc_by_rel_path.get(rel_path, {})
        scan_index = tag_t.get("index")

        # track_number is ALWAYS the continuous index scan_folder() assigns
        # across discs (spec: Multi-disc detection; CONTEXT: a per-disc
        # TRACKNUMBER must never reach track_number). Never trust the raw
        # TRACKNUMBER tag here -- unlike the interactive path, bulk_ingest runs
        # unattended over large libraries and a bad/partial tag must not
        # silently duplicate or skip a track number.
        track_number = scan_index
        if track_number is None:
            track_number = idx + 1

        tracks_out.append({
            "track_number":       track_number,
            "disc_number":        disc.get("disc_number"),
            "disc_track_number":  disc.get("disc_track_number"),
            "set_number":         disc.get("set_number"),
            "title":              tag_title or info_title or None,
            "duration":           tag_t.get("duration"),
            "filename":           tag_t.get("rel_path") or tag_t.get("filename"),
            "songwriter":         info_t.get("songwriter"),
        })

    return {
        "source_folder_path": payload.get("folder_path"),
        "artist_name":        extracted.get("artist"),
        "start_year":         extracted.get("year"),
        "start_month":        extracted.get("month"),
        "start_day":          extracted.get("day"),
        "venue_name":         extracted.get("venue"),
        "city":               extracted.get("city"),
        "state":              extracted.get("state"),
        "country":            extracted.get("country"),
        "source":             extracted.get("source"),
        "lineage":            extracted.get("lineage"),
        "source_tag":         extracted.get("source_tag"),
        "etree_shnid":        extracted.get("shnid"),
        "info_file_content":  payload.get("info_file_content"),
        "fingerprints":       payload.get("fingerprints"),
        "is_complete":        True,
        "skip_analysis":      True,
        "tracks":             tracks_out,
        "title":              extracted.get("album"),
        "kind":               kind,
    }
