"""
utils/ingest.py — Recording ingestion utilities.

Handles:
  - Scanning a source folder for audio, text, and fingerprint files
  - Reading existing FLAC tags via mutagen
  - Parsing the info/text file for metadata suggestions
  - Moving or copying the folder into the library
  - Writing the canonical folder name
"""

import os
import re
import shutil
import datetime
import unicodedata
from difflib import get_close_matches
from pathlib import Path
from mutagen.flac import FLAC
from mutagen import MutagenError
from mutagen.mp3 import MP3
from mutagen.id3 import TPE1, TPE2, TALB, TDRC, TIT2, TRCK, TPOS, TXXX
import geonamescache as _geonamescache

from app.utils.format import format_partial_date
from app.utils.reader.dates import best_show_date as _best_show_date
from app.utils.health import compute_health
from app.utils.folder_naming import unique_folder_name, unique_file_name
from app.utils.file_naming import rename_plan, flattens


# ── File classification ────────────────────────────────────────────────────────

# Only FLAC and MP3 are ever imported (Ryan, 2026-10-02). WAV, AIFF, SHN, APE
# and WV classify as unsupported (see UNSUPPORTED_AUDIO_EXTENSIONS below) and
# are offered Convert to FLAC instead.
AUDIO_EXTENSIONS    = {".flac", ".mp3"}
FINGERPRINT_MARKERS = {"ffp", "md5", "eac", "shntool", "fingerprint", "st5"}
TEXT_EXTENSION      = ".txt"

# Subdir names that indicate multi-set/disc folder structure. Matched
# case-insensitively against the subdir basename, in two forms.
#
# Traders spell the number as often as they digit it — Ryan hit `del01-09-22`
# with `disc one` / `disc two` subdirs (2026-08-12), which matched nothing, so
# the folder was read as a GROUPING folder and each disc queued as its own
# recording. Hence the word form.
_SET_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

# Digit form: "cd1", "Disc 2", "d-3". The bare "d" prefix is allowed here only.
_SET_DIGIT_RE = re.compile(
    r"^(cd|disc|disk|set|volume|vol|part|tape|show|d)\s*[-_]?\s*(\d+)$",
    re.IGNORECASE,
)

# Word form: "disc one", "Set_Two". A separator is REQUIRED and the bare "d"
# prefix excluded — otherwise a staging folder named "done" parses as "Disc 1".
_SET_WORD_RE = re.compile(
    r"^(cd|disc|disk|set|volume|vol|part|tape|show)[\s\-_]+("
    + "|".join(_SET_NUMBER_WORDS) + r")$",
    re.IGNORECASE,
)

_SET_PREFIX_LABELS = {
    "D": "Disc", "CD": "CD", "DISC": "Disc", "DISK": "Disc",
    "VOL": "Vol", "VOLUME": "Vol", "PART": "Part", "TAPE": "Tape",
    "SET": "Set", "SHOW": "Show",
}


# Prefixes that carry the DISC family (track.disc_number / disc_track_number).
# "set" is the only prefix in _SET_DIGIT_RE / _SET_WORD_RE that carries the
# SET family instead (track.set_number) — see section 1.4 of the spec: the
# two families never bridge.
_DISC_PREFIXES = {"D", "CD", "DISC", "DISK", "VOL", "VOLUME", "PART", "TAPE", "SHOW"}


def _parse_set_dir(name):
    """
    Parse a set/disc subdir name into (canonical_label, number, kind).
    'disc one' -> ('Disc 1', 1, 'disc');  'CD 02' -> ('CD 2', 2, 'disc');
    'Set 1' -> ('Set 1', 1, 'set').  A subdir literally named 'Encore'
    returns ('Encore', None, 'set').  None if no match.

    kind is 'disc' for cd/disc/disk/d/vol/volume/part/tape/show, and 'set'
    for set (and the literal Encore). Disc carriers fill
    track.disc_number/disc_track_number; set carriers fill track.set_number.
    The two families never bridge (spec section 1.4).

    Single source of truth for "is this folder a disc or set, and which one"
    — resolve_shows() and scan_folder() both ask it, so the triage queue and
    the ingest scanner can never disagree about whether a folder is one show.
    """
    name = (name or "").strip()
    if name == "Encore":
        return "Encore", None, "set"
    m = _SET_DIGIT_RE.match(name)
    if m:
        prefix, number = m.group(1), int(m.group(2))
    else:
        m = _SET_WORD_RE.match(name)
        if not m:
            return None
        prefix, number = m.group(1), _SET_NUMBER_WORDS[m.group(2).lower()]
    label = _SET_PREFIX_LABELS.get(prefix.upper(), prefix.title())
    kind  = "disc" if prefix.upper() in _DISC_PREFIXES else "set"
    return f"{label} {number}", number, kind

# Keywords that suggest a text file is the info/setlist file rather than
# a README or technical notes. Higher score = preferred.
_TEXT_PREFER_WORDS = {
    "setlist": 10, "set list": 10, "info": 8, "readme": -5,
    "lineage": 6,  "source": 4,   "notes": 3, "taper": 6,
    "show": 4,     "concert": 4,
}


# Broader than AUDIO_EXTENSIONS above (which governs what gets ingested as a
# track). Show RESOLUTION needs to recognise a folder as "containing audio" even
# for formats the ingest pipeline itself won't take, or a folder of .ape files
# looks like an empty grouping folder and gets silently walked past.
# .shn added 2026-08-26 (Ryan — a Shorten-sourced show from gdarchive.net):
# without it, a folder of nothing but .shn was completely invisible rather
# than recognised-but-unsupported, and "Add Recordings" reported a bare
# "no audio folders found" with nothing pointing at why.
RESOLVE_AUDIO_EXTS = {".flac", ".mp3", ".wav", ".aiff", ".aif",
                      ".m4a", ".ogg", ".ape", ".wv", ".shn"}

# Recognised as audio by RESOLVE_AUDIO_EXTS, but not a format the ingest
# pipeline can actually read a track from. scan_folder() sorts files matching
# this into their own bucket rather than the generic "other files" pile, so
# the UI can say what's actually wrong ("6 .shn files — not supported yet")
# instead of a show silently scoring zero with no explanation.
UNSUPPORTED_AUDIO_EXTENSIONS = RESOLVE_AUDIO_EXTS - AUDIO_EXTENSIONS


# Directory-name prefixes that are never a show and never part of one.
#
# Two families, both of which surfaced as red "no audio here" rows on every
# scan before 2026-09-17:
#
#   - Trellis's own namespaces under LIBRARY_ROOT: _musicians/, _venues/,
#     _events/, and the per-artist _images/ folder (app/api/artists.py).
#     Pointing the library at a collector's existing folder (rather than one
#     Trellis created) puts these in the same directory as their shows, so a
#     rescan offered one bogus row per photographed artist.
#   - Housekeeping: _originals/ from an SHN/WAV -> FLAC conversion, and
#     anything dot-prefixed (.git, Finder and sync-client metadata).
#
# Matching on the PREFIX rather than a name list is deliberate: the leading
# underscore is already the convention every Trellis-owned directory under
# LIBRARY_ROOT follows, so a bucket added later is covered here without
# anyone remembering that this list exists.
_EXCLUDED_DIR_PREFIXES = ("_", ".")


def _is_excluded_dir(name):
    """True when a directory name is one show resolution must never enter."""
    return name.startswith(_EXCLUDED_DIR_PREFIXES)


def _root_audio_count(path):
    """Count audio files directly in `path` (non-recursive)."""
    try:
        return sum(
            1 for f in os.scandir(path)
            if f.is_file()
            and os.path.splitext(f.name)[1].lower() in RESOLVE_AUDIO_EXTS
        )
    except OSError:
        return 0


def _audio_subdirs(path, unreadable=None):
    """
    Immediate subdirs of `path` that contain audio at any depth.

    Excluded directories (see _is_excluded_dir) are skipped outright, so an
    `_originals/` folder left by an SHN/WAV -> FLAC conversion can never be
    mistaken for a disc subdir or for a grouping child of its own show.

    `unreadable`, when given a list, collects (path, message) for any
    directory os.walk could not even scandir while descending through `path`
    -- a show folder nested below a perfectly readable artist/year folder
    (R2-1). Without this, os.walk's default onerror=None means a subdir
    that is itself unreadable (or that became so mid-walk) simply yields no
    files and looks identical to one that is genuinely empty of audio.
    """
    result = []
    try:
        for sub in os.scandir(path):
            if not sub.is_dir() or _is_excluded_dir(sub.name):
                continue
            def _onerror(exc, _sub=sub):
                if unreadable is not None:
                    unreadable.append((exc.filename or _sub.path, str(exc)))

            for _, _, files in os.walk(sub.path, onerror=_onerror):
                if any(os.path.splitext(f)[1].lower() in RESOLVE_AUDIO_EXTS
                       for f in files):
                    result.append(sub)
                    break
    except OSError as e:
        if unreadable is not None:
            unreadable.append((path, str(e)))
    return result


def _is_multi_disc(subs):
    """2+ audio-bearing subdirs named for a disc/set: the parent is ONE show.
    Shared by resolve_shows and is_show_root so they cannot disagree."""
    return sum(1 for s in subs if _parse_set_dir(s.name)) >= 2


def is_show_root(path):
    """True when `path` ITSELF is a show folder by resolve_shows' own rules
    (audio at its top level, or a multi-disc show whose set subdirs carry the
    audio), as opposed to a folder that merely contains shows. Bulk Ingest uses
    it for a run pointed at a single folder. Cheap: one level, no recursion."""
    try:
        if _root_audio_count(path) > 0:
            return True
        return _is_multi_disc(_audio_subdirs(path))
    except OSError:
        return False


def resolve_shows(path, include_empty=True, unreadable=None):
    """
    Recursively resolve a directory to its actual show-level paths.

    Hoisted out of `api/ingest.py::batch_scan`'s closure on 2026-07-30 so the
    Listening Quality analyser can resolve shows the SAME way batch scanning
    does. Two implementations of "what counts as a show" would drift, and the
    triage list disagreeing with the metadata list about which folders exist
    would be a genuinely confusing bug.

    Logic:
      - Has root audio → it's a show, return it.
      - Has >= 2 audio-containing subdirs → grouping folder, expand each.
      - Has exactly 1 audio-containing subdir → could be a transparent wrapper
        ('flac/') OR another nesting level; recurse to find out.
      - Has no audio at all → return as-is when `include_empty`, so a
        folder the USER named is reported (the scanner grades it red)
        rather than vanishing. A directory being WALKED during a scan
        passes include_empty=False instead — see resolve_shows_in_dir.

    `unreadable`, when given a list, is threaded down into every recursive
    call and into _audio_subdirs() so a show-shaped folder that is itself
    unreadable, however many readable ancestors sit above it, is collected
    rather than silently read as "no audio here" (R2-1).
    """
    if _root_audio_count(path) > 0:
        return [path]
    subs = _audio_subdirs(path, unreadable=unreadable)
    if not subs:
        return [path] if include_empty else []
    if len(subs) == 1:
        return resolve_shows(subs[0].path, include_empty, unreadable=unreadable)

    # A multi-disc show is ONE show, not a grouping folder. When 2+ of the
    # audio-bearing subdirs are named for a disc/set, the parent is the show
    # and scan_folder() will flatten the discs into it with continuous track
    # numbering. Expanding here instead queues each disc as its own recording
    # — Ryan's `del01-09-22` report, 2026-08-12.
    #
    # The threshold is deliberately identical to scan_folder's own
    # `len(set_dirs) >= 2`, and both count only audio-bearing subdirs, so the
    # triage queue and the ingest scanner cannot disagree about what a folder
    # is. A stray sibling that also holds audio (an "Extras" folder) does not
    # veto the call: treating one show as two recordings is a worse and less
    # recoverable outcome than one recording carrying an unlabelled extra.
    if _is_multi_disc(subs):
        return [path]

    result = []
    for sub in sorted(subs, key=lambda e: e.name.lower()):
        result.extend(resolve_shows(sub.path, include_empty, unreadable=unreadable))
    return result


def resolve_shows_in_dir(source_dir, skipped=None, unreadable=None):
    """
    Every show folder under one scanned directory.

    The top-level loop both `batch_scan` and the quality analyser run: each
    immediate subdirectory is resolved to its real show paths, handling
    arbitrary nesting (artist -> year -> show).

    Two kinds of directory are dropped rather than returned (2026-09-17, for
    libraries Trellis did not lay out itself):

      - Excluded names (see _is_excluded_dir) — Trellis's own _musicians/,
        _venues/ and _events/ buckets, and dot-prefixed metadata.
      - Anything with no audio anywhere beneath it. This is the one that
        matters in a flat library: an artist's photos live at
        LIBRARY_ROOT/<Artist>/_images, so `<Artist>/` is a plain directory
        whose only child is excluded, and it would otherwise come back as a
        show that grades red — one bogus row per photographed artist, on
        every rescan. It also covers stray `Art/` and notes folders.

    The distinction from resolve_shows() itself is the whole point: a folder
    the USER named and pointed at must still report "no audio here", because
    silently returning nothing for an explicit target is the kind of failure
    that reads as the app being broken. A folder merely encountered while
    walking has not been asked about and is simply not a show.

    `skipped`, when given a list, collects the audio-less paths that were
    dropped, so a bulk-ingest summary can say where the folders went instead
    of leaving a collector to wonder why 100 folders produced 40 shows.
    Excluded names are NOT collected — those are Trellis's own furniture,
    not the collector's missing material.

    `unreadable`, when given a list, collects (path, message) for a
    top-level entry that raised an OSError just being scanned (a chmod 000
    folder, an NFS mount that dropped mid-walk). S8: resolve_shows() itself
    swallows that same OSError deep inside _root_audio_count/_audio_subdirs
    and comes back looking exactly like "no audio here", so without this the
    folder silently vanishes -- a folder the app genuinely cannot read must
    be reported as failed, never dropped like an empty one.
    """
    show_paths = []
    for entry in sorted(os.scandir(source_dir), key=lambda e: e.name.lower()):
        if not entry.is_dir() or _is_excluded_dir(entry.name):
            continue
        if unreadable is not None:
            try:
                with os.scandir(entry.path) as it:
                    next(iter(it), None)
            except OSError as e:
                unreadable.append((entry.path, str(e)))
                continue
        resolved = resolve_shows(entry.path, include_empty=False, unreadable=unreadable)
        if not resolved and skipped is not None:
            skipped.append(entry.path)
        show_paths.extend(resolved)
    return show_paths


def _auto_set_label(subdir_name):
    """
    Convert a subdir name like 'cd1', 'Disc 2', 'disc one' into a canonical
    set label like 'CD 1', 'Disc 2', 'Disc 1'.  Returns None if no match.
    """
    parsed = _parse_set_dir(subdir_name)
    return parsed[0] if parsed else None


# Some sources are FLAT but encode the disc in the FILENAME rather than in a
# subdir — the etree convention `d01t01.`, `cd1t05`, `s2t03`, `cd1-04`. The
# subdir pass above sees one flat folder, reports sets_detected=False, and the
# pipeline then trusts each file's TRACKNUMBER tag — which resets per disc,
# producing two tracks numbered 1, two numbered 2, and so on. Same symptom as
# the 2026-07-14 CD1/CD2 bug, different carrier for the disc number.
# (Ryan, 2026-08-12 — Pat Metheny 1979-06-14, D01T01..D01T09 + D02T01..D02T07.)
#
# Anchored at the START of the basename, and _apply_filename_sets requires
# EVERY audio file to match plus 2+ distinct disc numbers: a partial match
# means the convention isn't really in use, and half-labelled sets are worse
# than none. Known limitation: a prefix embedded mid-name (`gd77-05-08d1t01`)
# is not detected — deliberately out of scope, one regex change if it shows up.
_FILENAME_SET_RE = re.compile(
    r"^(cd|d|s)\s*(\d{1,2})\s*(?:t|-|_)\s*(\d{1,3})(?=\D|$)",
    re.IGNORECASE,
)

# Leading digits at the start of a filename ("01.flac", "02 - Title.flac") —
# used to derive Track.disc_track_number from a disc-carrier filename when
# it carries its own number (spec section 2.2); falls back to 1-based sorted
# order within the disc folder when it doesn't.
_LEADING_NUM_RE = re.compile(r"^(\d{1,3})(?=\D|$)")


def _parse_filename_set(filename):
    """
    Parse a leading disc/track prefix off an audio filename.
    'D01T01. Show - Song.flac' -> ('Disc 1', 1, 1, 'disc').
    'S01T01. Show - Song.flac' -> ('Set 1', 1, 1, 'set').  None if no match.

    Reuses _auto_set_label so a filename-carried disc/set produces exactly
    the same label vocabulary as a subdir-carried one ('CD 1', 'Disc 2',
    'Set 1') — two sources of labels that disagree would surface as
    inconsistent names across otherwise identical recordings. kind is
    'disc' for the cd/d prefixes and 'set' for s (spec section 5); the two
    families never bridge.
    """
    m = _FILENAME_SET_RE.match(os.path.basename(filename).strip())
    if not m:
        return None
    prefix, disc, track = m.group(1).lower(), int(m.group(2)), int(m.group(3))
    # "s" is unambiguous only in this position; _parse_set_dir spells it "set".
    token = {"s": "set"}.get(prefix, prefix)
    label = _auto_set_label(f"{token}{disc}")
    if not label:
        return None
    kind = "set" if prefix == "s" else "disc"
    return label, disc, track, kind


def _apply_filename_sets(result):
    """
    Second-chance set detection for flat folders whose FILENAMES carry the
    disc or set (see _FILENAME_SET_RE). Mutates `result` in place: stamps
    disc_number/disc_track_number (kind 'disc') or set_number (kind 'set')
    on every audio file, re-sorts them into (number, track) order and
    renumbers `index` continuously across discs/sets. The two families never
    bridge (spec section 1.4) — a filename-encoded run is always one kind.

    That continuous index is the whole point — it is the contract subdir
    detection provides, and it is what tells the rest of the pipeline
    (read_source_tags -> the ingest wizard -> compute_audio_rename_map) to
    stop trusting per-disc TRACKNUMBER tags.

    No-op unless every audio file matches and 2+ distinct numbers are present.
    """
    audio = result["audio_files"]
    if not audio:
        return
    parsed = [_parse_filename_set(a["filename"]) for a in audio]
    if any(p is None for p in parsed):
        return
    if len({p[1] for p in parsed}) < 2:
        return   # a lone "d1" isn't multi-anything — same rule as subdirs

    order = sorted(
        zip(audio, parsed),
        key=lambda ap: (ap[1][1], ap[1][2], ap[0]["filename"].lower()),
    )
    result["audio_files"] = []
    for i, (a, (label, number, track, kind)) in enumerate(order, start=1):
        a["index"] = i
        if kind == "disc":
            a["disc_number"]       = number
            a["disc_track_number"] = track
        else:
            a["set_number"] = label
        result["audio_files"].append(a)

    result["sets_detected"] = True


def _int_tag(value):
    """Parse a tag value ("3", "03", or the leading side of "3/12") to an
    int, or None. Shared by the DISCNUMBER tag-carrier check below."""
    if value is None:
        return None
    part = str(value).strip().split("/")[0].strip()
    try:
        return int(part)
    except ValueError:
        return None


def _read_disc_tags(path):
    """
    (disc_number, track_number, disc_total) parsed from one audio file's
    DISCNUMBER/TRACKNUMBER/DISCTOTAL tags via open_tags(), or (None, None,
    None) when the file has no tags interface or no parseable DISCNUMBER.
    Scan-time only -- unrelated to read_flac_tags's own per-track reading.
    """
    try:
        audio = open_tags(path)
        if audio is None:
            return None, None, None
        disc = _int_tag(_first_tag(audio, ("DISCNUMBER",)))
        if disc is None:
            return None, None, None
        track = _int_tag(_first_tag(audio, ("TRACKNUMBER",)))
        total = _int_tag(_first_tag(audio, ("DISCTOTAL",)))
        return disc, track, total
    except (MutagenError, Exception):
        # Unreadable/corrupt file — same as "no DISCNUMBER tag" for this check.
        return None, None, None


def _apply_tag_disc_carrier(result):
    """
    DISCNUMBER tag as a disc carrier, last in line after subdir and filename
    carriers so it can override them.

    Precedence: the tag wins only when EVERY audio file in the scanned
    folder carries a parseable integer DISCNUMBER -- one untagged file
    leaves the existing subdir/filename carrier result (if any) completely
    unchanged. When the tag wins, each file's disc_number becomes its own
    DISCNUMBER and disc_track_number becomes its own integer TRACKNUMBER, or
    failing that, 1-based order within that disc as currently sorted. A
    folder where every file agrees on DISCNUMBER=1 is treated as multi-disc
    (disc_number=1, not null) only if a DISCTOTAL > 1 is present somewhere;
    otherwise it is a lone "1/1", not a multi-disc set, and disc fields are
    left exactly as the earlier carriers (or lack of one) left them.
    Continuous index/track_number is never touched here.
    """
    audio_files = result["audio_files"]
    if not audio_files:
        return

    tag_info = [_read_disc_tags(a["path"]) for a in audio_files]
    if any(disc is None for disc, _track, _total in tag_info):
        return   # not every file tagged -> tag ignored, carriers stand

    discs = {disc for disc, _track, _total in tag_info}
    if discs == {1}:
        totals = [total for _disc, _track, total in tag_info if total is not None]
        if not totals or max(totals) <= 1:
            return   # a lone "1/1" isn't a multi-disc set

    counters = {}
    for a, (disc, track, _total) in zip(audio_files, tag_info):
        a["disc_number"] = disc
        if track is not None:
            a["disc_track_number"] = track
        else:
            counters[disc] = counters.get(disc, 0) + 1
            a["disc_track_number"] = counters[disc]

    # This carrier just stamped every file with a disc_number the subdir/
    # filename carriers never set (or overrode), which is exactly what
    # sets_detected exists to report -- toTracks() in app.js and every other
    # multi-set reader trust this flag, not the presence of disc_number
    # alone, to decide index vs tag (see the other setter above, at the
    # subdir carrier).
    result["sets_detected"] = True


def _score_text_file(filename):
    """
    Return a preference score for a text file.  Higher → more likely to be
    the main info/setlist file.
    """
    low = filename.lower()
    score = 0
    for kw, pts in _TEXT_PREFER_WORDS.items():
        if kw in low:
            score += pts
    # Penalise very short filenames (e.g. 'md5.txt') — likely checksums
    if len(Path(filename).stem) <= 3:
        score -= 4
    # Bonus for files with a date pattern in the name (common in ROIO)
    if re.search(r"\d{4}[-_.]\d{2}[-_.]\d{2}", filename):
        score += 5
    return score


def _natural_key(name):
    """
    Sort key that splits digit runs out as numbers rather than characters,
    so an unpadded, hand-ripped folder ('t1.flac' .. 't10.flac') orders
    1, 2, ..., 10 instead of the lexical 1, 10, 2, ... 9 a bare sorted()
    gives it (R2-5). A zero-padded folder ('t01'..'t10') already sorted
    correctly either way and is unaffected. Used for every audio-file
    ordering scan_folder() produces, so a folder's track order is the same
    whichever door (interactive Add Recording or Bulk Ingest) it comes
    through -- and for the CD1/CD10-style subdir case, disc ordering
    already sorts on the parsed disc NUMBER (see set_dirs.sort below), not
    a name string, so it was never affected by this in the first place.
    """
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", name)]


def scan_folder(folder_path):
    """
    Walk a source folder and classify all files.

    Handles three structural cases:
      1. Flat folder — all audio in root (no subdirs with audio)
      2. Single transparent subdir — e.g. a 'flac/' subfolder; treated as flat
      3. Multi-set structure — subdirs named cd1/cd2, disc1/disc2, set1/set2, etc.
         Audio files get disc_number/disc_track_number (disc carriers) or
         set_number (set carriers) auto-populated from the subdir name. The
         two families never bridge (spec section 1.4).

    When multiple .txt files are present, the most likely info file is surfaced
    as text_files[0] based on filename scoring. All candidates are returned so
    the UI can offer a switcher.

    Returns:
      {
        "audio_files":       [ { index, filename, path,
                                  disc_number, disc_track_number, set_number } ],
        "text_files":        [ { filename, path, score } ],   # sorted best-first
        "fingerprints":      [ { type, filename, path } ],
        "unsupported_audio": [ { filename, path, ext } ],   # recognised audio format, but not one Trellis can read
        "other_files":       [ { filename, path } ],
        "sets_detected":     bool,   # True when multi-set subdir structure was used
      }
    """
    folder_path = str(folder_path)
    result = {
        "audio_files":       [],
        "text_files":        [],
        "fingerprints":      [],
        "unsupported_audio": [],   # recognised as audio, but a format we can't read a track from
        "other_files":       [],
        "sets_detected":     False,
    }

    # ── Detect subdir structure ────────────────────────────────────────────────
    try:
        top_entries = os.listdir(folder_path)
    except OSError:
        return result

    subdirs = [
        e for e in top_entries
        if os.path.isdir(os.path.join(folder_path, e)) and not e.startswith(".")
    ]
    root_audio = [
        f for f in top_entries
        if os.path.isfile(os.path.join(folder_path, f))
        and Path(f).suffix.lower() in AUDIO_EXTENSIONS
    ]

    # Multi-set detection: a subdir counts as a "set" (disc) only if its name
    # matches the CD/Disc/Set/etc pattern AND it actually contains audio —
    # named-but-empty subdirs (or a stray "Artwork" folder that happens to
    # match nothing) don't count. Ordered by the number in the name ("CD 2"
    # before "CD 10"), not alphabetically, so file/track order downstream is
    # deterministic regardless of filesystem listing order.
    # (2026-07-14: this detection previously computed a label via
    # _auto_set_label() but never actually used it — every file got
    # set=None regardless of folder structure, which is how a CD1/CD2 source
    # ended up with two tracks numbered 1-5 each: nothing here ever told the
    # rest of the pipeline the FLAC TRACKNUMBER tags reset per disc.)
    set_dirs = []   # [(abs_dirpath, label, number, kind)]
    for e in subdirs:
        parsed = _parse_set_dir(e)
        if not parsed:
            continue
        label, num, kind = parsed
        dpath = os.path.join(folder_path, e)
        try:
            has_audio = any(
                Path(f).suffix.lower() in AUDIO_EXTENSIONS
                for f in os.listdir(dpath)
                if os.path.isfile(os.path.join(dpath, f))
            )
        except OSError:
            has_audio = False
        if has_audio:
            set_dirs.append((dpath, label, num, kind))
    # Encore's number is None — sort it after every numbered set/disc.
    set_dirs.sort(key=lambda x: (x[2] is None, x[2]))
    sets_detected = len(set_dirs) >= 2   # one lone "Disc 1" folder isn't multi-anything
    result["sets_detected"] = sets_detected

    # Determine scan mode
    if not sets_detected and len(subdirs) == 1 and not root_audio:
        # Single transparent subdir (e.g. 'flac/') and not a recognized set —
        # treat as flat.
        scan_dirs = [(folder_path, None), (os.path.join(folder_path, subdirs[0]), None)]
    else:
        scan_dirs = None   # sentinel: use os.walk

    # ── File collection ────────────────────────────────────────────────────────
    audio_index = 0
    all_text    = []

    def _classify(fname, dirpath, disc_number=None, disc_track_number=None, set_number=None):
        nonlocal audio_index
        full = os.path.join(dirpath, fname)
        ext  = Path(fname).suffix.lower()
        low  = fname.lower()

        if ext in AUDIO_EXTENSIONS:
            audio_index += 1
            result["audio_files"].append({
                "index":    audio_index,
                "filename": fname,
                # rel_path is relative to the scan root — includes any subdir prefix
                # (e.g. "flac/01 - Dark Star.flac" or "CD 1/01.flac"). Ingest
                # flattens audio into the library folder root when the active
                # naming scheme calls for it (see compute_audio_rename_map /
                # move_to_library's flatten param) — rel_path here is only
                # used to locate the original file pre-flatten.
                "rel_path": os.path.relpath(full, folder_path),
                "path":     full,
                # disc_number/disc_track_number: None outside a disc carrier.
                # set_number: None outside a set carrier. The two families
                # never both hold a value for the same file (spec 1.4).
                "disc_number":       disc_number,
                "disc_track_number": disc_track_number,
                "set_number":        set_number,
            })
        elif ext == TEXT_EXTENSION:
            # Content-aware since 2026-08-02 — a checksum list named after the
            # show used to be scored as an info-file candidate and win.
            fp_type = fingerprint_type_for_file(full, low)
            if fp_type:
                result["fingerprints"].append({
                    "type":     fp_type,
                    "filename": fname,
                    "path":     full,
                    "rel_path": os.path.relpath(full, folder_path),
                })
            else:
                all_text.append({"filename": fname, "path": full})
        elif any(m in low for m in FINGERPRINT_MARKERS):
            result["fingerprints"].append({
                "type":     _detect_fp_type(low),
                "filename": fname,
                "path":     full,
                "rel_path": os.path.relpath(full, folder_path),
            })
        elif ext in UNSUPPORTED_AUDIO_EXTENSIONS:
            result["unsupported_audio"].append({
                "filename": fname, "path": full, "ext": ext,
            })
        else:
            result["other_files"].append({"filename": fname, "path": full})

    if sets_detected:
        # Deterministic order: root-level loose files first (rare), then each
        # detected set in numeric order (filenames sorted within each), then
        # a final sweep for anything else (e.g. "Art/") so other_files and
        # fingerprints located outside the set folders still get picked up —
        # skipping the set dirs themselves so nothing is double-counted.
        for fname in sorted(top_entries, key=_natural_key):
            full = os.path.join(folder_path, fname)
            if os.path.isfile(full):
                _classify(fname, folder_path)
        for dpath, label, num, kind in set_dirs:
            try:
                listing = sorted(os.listdir(dpath), key=_natural_key)
                if kind == "disc":
                    # disc_track_number: the filename's own leading number
                    # when EVERY audio file in this disc folder carries one
                    # (spec section 2.2) — a taper's date-first convention
                    # ("1977-05-08 - 01 - Title.flac") would otherwise let
                    # the leading digits parse as a bogus track number, so
                    # a parsed number is trusted only when the whole disc
                    # agrees; one file with no leading number falls the
                    # entire disc back to 1-based counter order. A per-disc
                    # checksum file sorting ahead of "01.flac" is excluded —
                    # only audio files count toward this decision.
                    audio_names = [f for f in listing
                                   if Path(f).suffix.lower() in AUDIO_EXTENSIONS]
                    all_parse = bool(audio_names) and all(
                        _LEADING_NUM_RE.match(f) for f in audio_names)
                disc_track_counter = 0
                for fname in listing:
                    full = os.path.join(dpath, fname)
                    if not os.path.isfile(full):
                        continue
                    is_audio = Path(fname).suffix.lower() in AUDIO_EXTENSIONS
                    if kind == "disc" and is_audio:
                        if all_parse:
                            disc_track_number = int(_LEADING_NUM_RE.match(fname).group(1))
                        else:
                            disc_track_counter += 1
                            disc_track_number = disc_track_counter
                        _classify(fname, dpath, disc_number=num,
                                  disc_track_number=disc_track_number)
                    elif kind == "set":
                        _classify(fname, dpath, set_number=label)
                    else:
                        _classify(fname, dpath)
            except OSError:
                pass
        set_dir_paths = {dpath for dpath, _label, _num, _kind in set_dirs}
        for dirpath, dirnames, filenames in os.walk(folder_path):
            if dirpath == folder_path:
                dirnames[:] = [d for d in dirnames
                               if os.path.join(dirpath, d) not in set_dir_paths]
                continue   # root files already classified above
            for fname in sorted(filenames, key=_natural_key):
                _classify(fname, dirpath)
    elif scan_dirs is not None:
        # Structured walk: visit each (dir, set_label) pair, non-recursive
        seen_dirs = set()
        for dir_path, set_label in scan_dirs:
            if dir_path in seen_dirs:
                continue
            seen_dirs.add(dir_path)
            try:
                for fname in sorted(os.listdir(dir_path), key=_natural_key):
                    full = os.path.join(dir_path, fname)
                    if os.path.isfile(full):
                        _classify(fname, dir_path)
            except OSError:
                pass
    else:
        # Flat walk
        for dirpath, _, filenames in os.walk(folder_path):
            for fname in sorted(filenames, key=_natural_key):
                _classify(fname, dirpath)

    # ── Filename-encoded sets ─────────────────────────────────────────────────
    # Flat folder, disc in the filename (d01t01…). Runs only when the subdir
    # pass found nothing, and after the walk so it can renumber the finished
    # audio list rather than restructure the traversal.
    if not sets_detected:
        _apply_filename_sets(result)

    # ── DISCNUMBER tag carrier ──────────────────────────────────────────────
    # Runs last so a fully-tagged source can override subdir/filename
    # carriers (or supply disc info where neither fired). See
    # _apply_tag_disc_carrier's docstring for the exact precedence.
    _apply_tag_disc_carrier(result)

    # ── Score and sort text files ──────────────────────────────────────────────
    for tf in all_text:
        tf["score"] = _score_text_file(tf["filename"])
    all_text.sort(key=lambda x: x["score"], reverse=True)
    result["text_files"] = all_text

    return result


def _detect_fp_type(filename_lower):
    # st5 checked first: shntool's own checksum is, by design, the same MD5-of-
    # decoded-audio value as an ffp (see app/utils/checksums.py docstring) — but
    # a filename like "checksum.st5" or "*_shntool.md5" should still resolve to
    # st5, not be mistaken for a plain whole-file md5.
    if filename_lower.endswith(".st5") or "st5" in filename_lower or "shntool" in filename_lower:
        return "st5"
    if "ffp" in filename_lower:
        return "ffp"
    if "md5" in filename_lower:
        return "md5"
    return "other"


# A checksum file is not obliged to announce itself in its filename. Ryan hit
# a show whose md5 list was named `AoifeODonovan2012-08-19_MCE400_16bit.txt`,
# sitting beside the real info file `Aoife O'Donovan Band.txt` (2026-08-02).
# Nothing in that name matches FINGERPRINT_MARKERS, so it was filed as a text
# candidate — and then WON the info-file scoring, because _score_text_file
# awards +5 for a date pattern in the name and the genuine info file scored 0.
# The ingest form came up with a 32-hex hash in the Venue box.
#
# Two bugs in one: the wrong file was read for metadata, AND the checksums were
# never registered, so that recording would have ingested unverified.
#
# The fix is to look INSIDE the file rather than trust its name. Content is the
# authority; the filename is a hint.
_FP_SNIFF_MAX_BYTES = 262144   # a checksum list is tiny; a big .txt is prose
_FP_SNIFF_MIN_LINES = 2
_FP_SNIFF_RATIO     = 0.6      # share of non-blank lines that must be checksums
_HEX32_AT_END       = re.compile(r"[0-9a-fA-F]{32}\s*$")


def _sniff_fingerprint_type(path):
    """
    Read a .txt and decide whether it is really a checksum list. Returns
    "ffp" / "md5" / None.

    Reuses checksums.parse_checksum_file() rather than inventing a second
    line format — that parser already tolerates every delimiter the community
    tools emit (colon, tab, double space, "*"-prefixed filename), and having
    two disagreeing notions of "is this a checksum line" is exactly the kind of
    split-brain that produces bugs like this one.

    The RATIO test is what keeps an info file that happens to quote a few
    hashes from being swallowed: a genuine checksum list is essentially nothing
    but hashes, while a setlist with a lineage note is mostly prose.
    """
    try:
        if os.path.getsize(path) > _FP_SNIFF_MAX_BYTES:
            return None
        content = _read_text_auto(path)
    except OSError:
        return None
    if not content:
        return None

    lines = [ln for ln in (l.strip() for l in content.splitlines()) if ln]
    if len(lines) < _FP_SNIFF_MIN_LINES:
        return None

    from app.utils.checksums import parse_checksum_file
    entries = parse_checksum_file(content)
    if len(entries) < _FP_SNIFF_MIN_LINES or len(entries) < _FP_SNIFF_RATIO * len(lines):
        return None

    # Shape tells the two apart. ffp puts the hash LAST ("track.flac:abc123…");
    # md5sum puts it FIRST ("abc123… *track.flac"). Never guess st5 from
    # content — st5 is byte-identical to ffp, and it is the lowest-priority
    # type (see FINGERPRINT_TYPE_PRIORITY), so guessing it would demote a
    # perfectly good ffp. Ryan's standing preference: trust ffp and md5.
    trailing = sum(1 for ln in lines if _HEX32_AT_END.search(ln))
    return "ffp" if trailing >= len(entries) * 0.5 else "md5"


def fingerprint_type_for_file(path, filename_lower=None):
    """
    The single answer to "is this file a checksum list, and of what type?".

    Filename markers first (explicit and cheap), then a content sniff for the
    .txt files that carry no marker. Shared by scan_folder() and
    discover_fingerprint_files() so a fresh ingest and a post-hoc backfill
    classify the same file identically — which the latter's docstring has
    always promised.
    """
    low = filename_lower if filename_lower is not None else os.path.basename(path).lower()
    if any(m in low for m in FINGERPRINT_MARKERS):
        return _detect_fp_type(low)
    if low.endswith(TEXT_EXTENSION):
        return _sniff_fingerprint_type(path)
    return None


# ── FLAC tag reading ───────────────────────────────────────────────────────────

# FLAC tag keys → our container field names. The current key comes first; the
# retired keys follow because every file tagged before 2026-09-16 carries them
# and other collectors use some of them. First non-empty value wins.
_CONTAINER_TAG_KEYS = {
    "artist":   ("ARTIST", "ALBUMARTIST"),
    "album":    ("ALBUM",),
    "venue":    ("VENUE", "CONCERTVENUE"),
    "location": ("LOCATION", "CONCERTLOCATION"),
    "source":   ("SOURCE", "RECORDINGSOURCE"),
    "lineage":  ("LINEAGE",),
}
# DATE held only the year until 2026-09-16 and CONCERTDATE the full date; now
# DATE holds the full date. Both are read and the more precise value wins.
_DATE_TAG_KEYS = ("DATE", "CONCERTDATE")

_ISO_PARTIAL_RE  = re.compile(r"^\s*(\d{4})(?:[-./](\d{1,2})(?:[-./](\d{1,2}))?)?\s*$")
_DATED_VENUE_RE  = re.compile(r"^\s*(\d{4}[-./]\d{1,2}[-./]\d{1,2})\s+-\s+(.+)$")


def _loose_tag_date(value):
    """
    Normalise a date tag to "YYYY", "YYYY-MM" or "YYYY-MM-DD", or None.
    Seen in the wild: "1977-05-08", "2015.02.27", "2026", "August 19, 2012".
    """
    if not value:
        return None
    m = _ISO_PARTIAL_RE.match(str(value))
    if m:
        y, mo, d = (int(g) if g else None for g in m.groups())
        if mo and not 1 <= mo <= 12:
            return None
        if d and not 1 <= d <= 31:
            return None
        return format_partial_date(y, mo, d)
    # Routed through the strict grammar (2026-10-03): a tag that writes only a
    # month and year stays "YYYY-MM"; nothing is filled from today's date.
    best, _ = _best_show_date(str(value))
    return format_partial_date(best.year, best.month, best.day) if best else None


def _first_tag(tags, keys):
    for k in keys:
        v = tags.get(k)
        if v and str(v[0]).strip():
            return str(v[0]).strip()
    return None


def _container_from_tags(tags):
    """Container fields from one file's Vorbis comments (see _CONTAINER_TAG_KEYS)."""
    out = {}
    for field, keys in _CONTAINER_TAG_KEYS.items():
        v = _first_tag(tags, keys)
        if v:
            out[field] = v

    dates = [d for d in (_loose_tag_date(_first_tag(tags, (k,))) for k in _DATE_TAG_KEYS) if d]
    if dates:
        best = max(dates, key=len)             # "1977-05-08" beats "1977"
        out["concert_date"] = best
        out["year"] = best[:4]

    # A VENUE carrying "2015.02.27 - Ryman Auditorium - Nashville, TN" (seen in
    # the wild): the date and place fill fields the file left empty.
    venue = out.get("venue")
    m = _DATED_VENUE_RE.match(venue) if venue else None
    if m:
        parts = [x.strip() for x in m.group(2).split(" - ") if x.strip()]
        if parts:
            out["venue"] = parts[0]
            if len(parts) > 1 and not out.get("location"):
                out["location"] = " - ".join(parts[1:])
        if not out.get("concert_date"):
            d = _loose_tag_date(m.group(1))
            if d:
                out["concert_date"], out["year"] = d, d[:4]
    return out


# ── One tag opener for FLAC and MP3 ────────────────────────────────────────

# ID3 frame classes with a direct one-to-one Vorbis-key equivalent, both
# directions (frame.FrameID == the class name, e.g. TPE1.FrameID == "TPE1").
# TRCK/TPOS are ID3's own "n/total" pair frames, split across two of our
# keys. Anything else -- VENUE, LOCATION, SOURCE, LINEAGE, CONCERTDATE,
# CONCERTVENUE, CONCERTLOCATION, RECORDINGSOURCE, and any future key -- rides
# in a TXXX frame keyed by description, so a new DB field never needs a new
# entry here.
_ID3_SIMPLE = {"ARTIST": TPE1, "ALBUMARTIST": TPE2, "ALBUM": TALB,
               "DATE": TDRC, "TITLE": TIT2}
_ID3_PAIR   = {"TRACKNUMBER": ("TRCK", 0), "TRACKTOTAL": ("TRCK", 1),
               "DISCNUMBER":  ("TPOS", 0), "DISCTOTAL":  ("TPOS", 1)}
_ID3_PAIR_CLASS = {"TRCK": TRCK, "TPOS": TPOS}

# Every non-simple, non-pair key Trellis ever writes into a TXXX frame (see
# every "container_tags[...] =" / "audio[...] =" assignment in this module).
# clear() only removes TXXX frames whose desc is one of these -- a foreign
# TXXX (someone else's tagger, e.g. "MY_CUSTOM") is left alone, matching
# FLAC.clear()'s blast radius (Vorbis block only, pictures untouched).
_TXXX_KEYS_WE_OWN = {"VENUE", "LOCATION", "SOURCE", "LINEAGE", "GENRE",
                     "PERFORMER", "COMPOSER", "COMMENT"}


class _MP3TagAdapter:
    """
    Makes an MP3's ID3 tag look like the FLAC object the rest of ingest.py
    already knows how to drive: get/__getitem__/__setitem__/__contains__/
    keys()/items()/clear()/save(), plus .info (duration), via the map above.
    Values are always lists of strings, matching mutagen's own Vorbis-comment
    shape, so read_flac_tags/read_recording_tags/write_flac_tags don't need
    to know which format they are looking at.
    """

    def __init__(self, path):
        self._mp3 = MP3(path)
        if self._mp3.tags is None:
            self._mp3.add_tags()
        self._id3 = self._mp3.tags
        self.info = self._mp3.info

    def _pair_parts(self, frame_id):
        frame = self._id3.get(frame_id)
        text = str(frame.text[0]) if frame and frame.text else ""
        parts = text.split("/")
        return (parts[0] or None, parts[1] if len(parts) > 1 and parts[1] else None)

    def _pair_set(self, frame_id, index, value):
        parts = list(self._pair_parts(frame_id))
        parts[index] = value
        num, total = parts
        cls = _ID3_PAIR_CLASS[frame_id]
        if not num and not total:
            self._id3.delall(frame_id)
            return
        text = num or ""
        if total:
            text = f"{text}/{total}"
        self._id3.setall(frame_id, [cls(encoding=3, text=[text])])

    def get(self, key, default=None):
        if key in _ID3_SIMPLE:
            frame = self._id3.get(_ID3_SIMPLE[key].__name__)
            return [str(frame.text[0])] if frame and frame.text else default
        if key in _ID3_PAIR:
            frame_id, idx = _ID3_PAIR[key]
            val = self._pair_parts(frame_id)[idx]
            return [val] if val else default
        for frame in self._id3.getall("TXXX"):
            if frame.desc == key:
                return [str(v) for v in frame.text] if frame.text else default
        return default

    def __getitem__(self, key):
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __setitem__(self, key, value):
        values = value if isinstance(value, list) else [value]
        values = [str(v) for v in values]
        if key in _ID3_SIMPLE:
            cls = _ID3_SIMPLE[key]
            self._id3.setall(cls.__name__, [cls(encoding=3, text=values)])
        elif key in _ID3_PAIR:
            frame_id, idx = _ID3_PAIR[key]
            self._pair_set(frame_id, idx, values[0] if values else None)
        else:
            self._id3.delall(f"TXXX:{key}")
            self._id3.add(TXXX(encoding=3, desc=key, text=values))

    def __contains__(self, key):
        return self.get(key) is not None

    def keys(self):
        out = [k for k in _ID3_SIMPLE if k in self]
        out += [k for k in _ID3_PAIR if k in self]
        out += [frame.desc for frame in self._id3.getall("TXXX")]
        return out

    def items(self):
        return [(k, self.get(k)) for k in self.keys()]

    def clear(self):
        # ID3.clear() removes EVERYTHING -- APIC (cover art), USLT and any
        # foreign TXXX frame along with the frames we actually own. Only
        # remove what write_flac_tags() is about to rewrite: the simple and
        # pair frames, plus TXXX frames whose desc is one of our own keys.
        # Matches FLAC.clear(), which removes only the Vorbis comment block
        # and keeps pictures.
        for cls in _ID3_SIMPLE.values():
            self._id3.delall(cls.__name__)
        for frame_id in _ID3_PAIR_CLASS:
            self._id3.delall(frame_id)
        for key in _TXXX_KEYS_WE_OWN:
            self._id3.delall(f"TXXX:{key}")

    def save(self):
        self._mp3.save()


def open_tags(path):
    """
    Open path's tags through one door regardless of format. FLAC files
    return mutagen's native FLAC object, unchanged from before. MP3s return
    _MP3TagAdapter, an ID3-backed stand-in presenting the identical
    dict-like shape via the map above. Anything else (.wav, unrecognised)
    returns None -- callers skip files with no tags.
    """
    ext = Path(path).suffix.lower()
    if ext == ".flac":
        return FLAC(path)
    if ext == ".mp3":
        return _MP3TagAdapter(path)
    return None


def read_flac_tags(audio_files):
    """
    Read FLAC tags from all audio files.

    Returns:
      {
        "container": { artist, album, year, concert_date, venue, location, source, lineage },
        "tracks":    [ { index, filename, title, track_number, duration } ]
      }
    Container fields are read from the first successfully tagged file.
    """
    container = {}
    tracks    = []

    for f in audio_files:
        path = f["path"]
        try:
            audio = open_tags(path)
            if audio is None:
                # No tags interface for this format (e.g. .wav) — placeholder
                # so index stays consistent (same path as an unreadable file).
                raise MutagenError("no tags interface for this format")
            tags = audio

            # Capture container-level tags from first file that has them
            if not container:
                container = _container_from_tags(tags)

            # Full raw tags (lowercased keys, single values unwrapped) so the
            # UI can show the same JSON as the recording view's File Tags.
            raw = {k.lower(): (v[0] if isinstance(v, list) and len(v) == 1 else v)
                   for k, v in tags.items()}

            # Track-level
            track_entry = {
                "index":        f["index"],
                "filename":     f["filename"],
                "rel_path":     f.get("rel_path", f["filename"]),
                "title":        tags.get("TITLE",       [None])[0],
                "track_number": tags.get("TRACKNUMBER", [None])[0],
                "duration":     int(audio.info.length) if audio.info else None,
                "raw":          raw,
            }
            tracks.append(track_entry)

        except (MutagenError, Exception) as e:
            # Unreadable file — add placeholder so index stays consistent.
            # "raw": None (never {}) marks a file that could not even be
            # opened (R2-N3) -- bulk_ingest.py's extract() reads this instead
            # of opening every readable file a second time itself just to
            # find out the same thing.
            tracks.append({
                "index":        f["index"],
                "filename":     f["filename"],
                "rel_path":     f.get("rel_path", f["filename"]),
                "title":        None,
                "track_number": None,
                "duration":     None,
                "raw":          None,
                "open_error":   str(e),
            })

    return {"container": container, "tracks": tracks}


# ── FLAC tag writing ───────────────────────────────────────────────────────────

def build_recording_tags(recording):
    """
    Build the container-level Vorbis comment dict for a recording from its
    Recording → Performance → Artist → Venue chain. Single source of truth
    for the DB→tag mapping, shared by write_flac_tags (which writes it to disk)
    and the debug endpoint (which compares it against on-disk tags).

    Returns (container_tags: dict, track_total: str). Only non-empty values are
    included in container_tags. PERFORMER is a list (one Vorbis comment per
    musician); every other value is a string.
    """
    perf   = recording.performance
    venue  = perf.venue if perf else None
    tracks = recording.tracks

    # ── Concert date string ───────────────────────────────────────────────────
    concert_date = format_partial_date(
        perf.start_year, perf.start_month, perf.start_day) if perf else None

    # ── Venue name + location ─────────────────────────────────────────────────
    venue_name = venue.name if venue else None
    if venue:
        location_parts = [p for p in [venue.city, venue.state, venue.country] if p]
    elif perf:
        location_parts = [p for p in [perf.city, perf.state, perf.country] if p]
    else:
        location_parts = []

    # ── Source string ─────────────────────────────────────────────────────────
    source_str = recording.source

    # ── Artist, genre, lineup ─────────────────────────────────────────────────
    artist      = perf.artist if perf else None
    artist_name = artist.name if artist else None
    genre_name  = artist.genre.name if (artist and artist.genre) else None
    # The lineup belongs to the PERFORMANCE (two recordings of one night share
    # it): roster inheritance, overrides, guests and dedupe are all resolved
    # there. Bare names, no instrument, in resolved order.
    performers = []
    if perf:
        from app.utils.personnel import resolve_performance_personnel
        performers = [p["name"] for p in resolve_performance_personnel(perf) if p.get("name")]

    # ALBUM is "date - venue": the act is already in ARTIST, and date first
    # makes players sort shows chronologically.
    album_parts = [p for p in [concert_date, venue_name] if p]
    album_str   = " - ".join(album_parts) if album_parts else None

    container_tags = {}
    if artist_name:       container_tags["ARTIST"]      = artist_name
    if artist_name:       container_tags["ALBUMARTIST"] = artist_name
    if album_str:         container_tags["ALBUM"]       = album_str
    if concert_date:      container_tags["DATE"]        = concert_date
    if venue_name:        container_tags["VENUE"]       = venue_name
    if location_parts:    container_tags["LOCATION"]    = ", ".join(location_parts)
    if source_str:        container_tags["SOURCE"]      = source_str
    if recording.lineage: container_tags["LINEAGE"]     = recording.lineage
    if genre_name:        container_tags["GENRE"]       = genre_name
    if performers:        container_tags["PERFORMER"]   = performers   # multi-valued

    return container_tags, str(len(tracks))


def read_recording_tags(recording, library_root):
    """
    Read the actual on-disk Vorbis comments from every FLAC file in a recording.

    Returns a list of {track_number, title, tags, error}. Multi-valued Vorbis
    comments are kept as lists; single values are unwrapped. Never exposes file
    paths (frontend obfuscation). Shared by the user-facing tags viewer and the
    dev debug endpoint.
    """
    out = []
    for track in sorted(recording.tracks, key=lambda t: t.track_number):
        abs_path = os.path.join(library_root, recording.folder_path, track.file_path)
        entry = {"track_number": track.track_number, "title": track.title,
                 "tags": None, "error": None}
        try:
            audio = open_tags(abs_path)
            if audio is None:
                entry["error"] = "Unsupported audio format"
            else:
                # N6: mutagen's native FLAC dict yields lowercase Vorbis-comment
                # keys ("venue") while _MP3TagAdapter always yields uppercase
                # ("VENUE") -- normalise to uppercase here so the tags pane
                # does not differ by format.
                entry["tags"] = {k.upper(): (v[0] if len(v) == 1 else v)
                                 for k, v in audio.items()}
        except FileNotFoundError:
            entry["error"] = "File not found"
        except MutagenError as e:
            entry["error"] = f"Mutagen: {e}"
        except Exception as e:                       # noqa: BLE001 — surface any read error
            entry["error"] = f"Error: {e}"
        out.append(entry)
    return out


def write_flac_tags(recording, library_root):
    """
    Write Vorbis comments from DB records to every FLAC file in a recording.

    Builds container-level tags via build_recording_tags(), then per-track
    TITLE/TRACKNUMBER/TRACKTOTAL for each Track. Existing Vorbis comments are
    replaced entirely (clean write, Ryan 2026-09-16). A track's note becomes
    COMMENT. DISCNUMBER/DISCTOTAL are written whenever any track carries a
    disc_number (DISCNUMBER = that track's disc_number, DISCTOTAL =
    max(disc_number) across the recording); a single-disc recording gets
    neither. Anything else we do not write is removed.

    Args:
        recording:    Recording ORM object with relationships loaded
        library_root: Absolute path string for the library root

    Returns:
        (n_written, errors) where errors is a list of (filename, message) tuples.
    """
    tracks = recording.tracks  # ordered by track_number via relationship
    container_tags, track_total = build_recording_tags(recording)

    disc_numbers = [t.disc_number for t in tracks if t.disc_number is not None]
    disc_total   = max(disc_numbers) if disc_numbers else None

    n_written = 0
    errors    = []

    for track in tracks:
        abs_path = os.path.join(library_root, recording.folder_path, track.file_path)
        try:
            audio = open_tags(abs_path)
            if audio is None:
                raise MutagenError("Unsupported audio format")

            # Clear all existing tags
            audio.clear()

            # Container tags
            for tag_key, value in container_tags.items():
                audio[tag_key] = value

            # Track-specific tags
            audio["TITLE"]       = track.title
            audio["TRACKNUMBER"] = str(track.track_number)
            audio["TRACKTOTAL"]  = track_total
            if track.disc_number is not None:
                audio["DISCNUMBER"] = str(track.disc_number)
                audio["DISCTOTAL"]  = str(disc_total)
            if track.songwriter:
                audio["COMPOSER"] = track.songwriter
            if track.notes and track.notes.strip():
                audio["COMMENT"] = track.notes.strip()

            audio.save()
            n_written += 1

        except FileNotFoundError:
            errors.append((track.file_path, "File not found"))
        except MutagenError as e:
            errors.append((track.file_path, f"Mutagen error: {e}"))
        except Exception as e:
            errors.append((track.file_path, f"Unexpected error: {e}"))

    return n_written, errors


# ── Info file parsing ──────────────────────────────────────────────────────────

# Initialise geonamescache once at import time (pure local JSON, fast)
_gc             = _geonamescache.GeonamesCache()
_CITY_NAMES     = {c["name"].lower() for c in _gc.get_cities().values()}
_US_STATES      = _gc.get_us_states()                                      # {CA: {name:"California",...}}
_US_STATE_CODES = set(_US_STATES.keys())                                   # {"CA","NY",...}
_US_STATE_NAMES = {v["name"].lower(): k for k, v in _US_STATES.items()}   # {"california":"CA",...}
_COUNTRIES      = _gc.get_countries()
_COUNTRY_NAMES  = {v["name"].lower() for v in _COUNTRIES.values()}
# Common country spellings the gazetteer stores under a different canonical
# name, mapped to the short form we store.
_COUNTRY_ALIASES = {
    "us": "US", "u.s.": "US", "u.s.a.": "US", "usa": "US",
    "united states": "US", "united states of america": "US", "america": "US",
    "uk": "UK", "u.k.": "UK", "united kingdom": "UK",
    "great britain": "UK", "britain": "UK", "england": "UK",
}

_CURRENT_YEAR   = datetime.date.today().year

# Month names for date-signal detection
_MONTH_NAMES = {
    "january","february","march","april","may","june",
    "july","august","september","october","november","december",
    "jan","feb","mar","apr","jun","jul","aug","sep","oct","nov","dec",
}

# Track line: "01 Title", "1. Title", "1 - Title", "11: Title"
_TRACK_PATTERN = re.compile(r"^\s*(\d{1,3})[.:\-\s]\s*(.+)$")

# A bare "H:MM:SS" or "M:SS" value with nothing else on the line — almost
# always a stated total running time in the header ("1:46:28"), never a
# track. _TRACK_PATTERN alone can't tell these apart: it reads "1:46:28" as
# track 1 titled "46:28", which displaces every real track number by one
# (Ryan, 2026-08-30, from a Gary Burton Quintet info file whose 4th line was
# the recording's total length). Checked before a line is ever offered to
# _TRACK_PATTERN, so a bare duration line never becomes a track candidate in
# the first place — real track lines always carry a title too, so they never
# collide with this.
_TOTAL_DURATION_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")

# A standalone heading (nothing else on the line) that marks the end of the
# track list — trailing "Notes:"/"Comments:" sections often contain their own
# numbered lines (e.g. "1 - Noise at 2:51 from the guys goofing around.")
# which look exactly like track lines to _TRACK_PATTERN but are not tracks
# (Ryan, 2026-07-16).
_TRACKLIST_END_RE = re.compile(
    r"^(notes?|comments?|credits?|lineage|taping\s*notes?|equipment|thanks|acknowledge?ments?)\s*:?\s*$",
    re.IGNORECASE,
)

# Trailing timestamp appended by tapers: "Dark Star 12:34", "Intro :45", "Help > Slip 1:23:45"
_TRAILING_TS_RE = re.compile(r'\s+\d*:[\d:]+$')

# Trailing timestamp given in PARENTHESES instead — just as common a taper
# habit: "Carry On (4:59)". Only strips when the parens hold nothing but a
# time (digits and colons), so a genuine credit like "(Carla Bley)" is left
# alone (Ryan, 2026-08-30).
_TRAILING_PAREN_TS_RE = re.compile(r'\s*\(\d{1,2}:\d{2}(?::\d{2})?\)\s*$')

# Words kept lowercase in title case (unless first word)
_TC_LOWER = frozenset({
    'a', 'an', 'the', 'and', 'but', 'or', 'for', 'nor', 'at', 'by',
    'in', 'of', 'on', 'to', 'up', 'as', 'is', 'it', 'if', 'so', 'vs',
})

_TC_INITIALS_RE = re.compile(r'^(\W*)([^\W\d_](?:\.[^\W\d_])+)(\.?\W*)$')
_TC_APOS = "'\u2019"


def _tc_first_alpha_upper(w):
    for i, ch in enumerate(w):
        if ch.isalpha():
            return w[:i] + ch.upper() + w[i + 1:]
    return w


def title_case(s):
    """The one title-case function (artist names, track titles, venues).

    ALL-upper or ALL-lower input is lowercased first; mixed-case input keeps
    each word's own tail ("JGB", "McCoury"). Then per word: first letter
    capitalized past leading punctuation, minor words lowered (not first),
    initials ("j.d." -> "J.D."), Mc + letter, a single-letter apostrophe
    prefix ("o'donovan" -> "O'Donovan"; "i'm" is left alone), and the letter
    after a hyphen. str.title() is not used: it turns "Don't" into "Don'T".
    """
    if not s:
        return s
    letters = [c for c in s if c.isalpha()]
    normalize = bool(letters) and (all(c.isupper() for c in letters)
                                   or all(c.islower() for c in letters))
    out = []
    for i, w in enumerate(s.split()):
        if normalize:
            w = w.lower()
        m = _TC_INITIALS_RE.match(w)
        if m:
            out.append(m.group(1) + m.group(2).upper() + m.group(3))
            continue
        core = w.rstrip('.,;:!?)"\'\u2019\u201d')
        if i > 0 and core and core[0].isalnum() and core.lower() in _TC_LOWER:
            out.append(w.lower())
            continue
        w = _tc_first_alpha_upper(w)
        # Mc + letter (not Mac: Mack, Macon)
        j = next((k for k, ch in enumerate(w) if ch.isalnum()), None)
        if j is not None:
            tok = w[j:].rstrip('.,;:!?)"\'\u2019\u201d')
            if len(tok) >= 4 and tok[:2].lower() == 'mc' and tok[2].isalpha():
                w = w[:j] + 'Mc' + w[j + 2].upper() + w[j + 3:]
            # single letter + apostrophe + letter (O'Donovan, D'Angelo)
            if (tok[:1].isalpha() and tok[:1].lower() != 'i' and len(tok) >= 3
                    and tok[1] in _TC_APOS and tok[2].isalpha()):
                w = w[:j + 2] + w[j + 2].upper() + w[j + 3:]
        # letter after a hyphen
        w = re.sub(r'-([^\W\d_])', lambda mm: '-' + mm.group(1).upper(), w)
        out.append(w)
    return ' '.join(out)


# ── Track flag auto-detection ──────────────────────────────────────────────────
#
# Suggests NON_MUSIC_FLAGS-style flags from a track's title text alone. Kept
# deliberately conservative: several words that indicate a non-music segment
# ("talk", "speak", "crowd") also show up in real song titles ("Don't Talk",
# "Speak Low"), so this only fires on whole-word/whole-segment matches for the
# ambiguous cases, not loose substring checks. These are *suggestions* the
# archivist approves in the ingest wizard — not applied silently.
#
# Structural markers (used by Grateful Dead and others):
#   "// Title"  -> leading "//"  = start_truncated
#   "Title //"  -> trailing "//" = end_truncated
#   "Title (x)" -> trailing "(x)" (any case) = incomplete
#
# Keyword segments: a title is split on " and "/","/"/"/"&" (after stripping
# one trailing parenthetical, e.g. "(Bobby)") so compound titles like
# "tuning and banter (Bobby)" resolve to both ['tuning', 'banter'].

_FLAG_START_TRUNC = re.compile(r'^\s*//')
_FLAG_END_TRUNC   = re.compile(r'//\s*$')
_FLAG_INCOMPLETE  = re.compile(r'\(\s*x\s*\)\s*$', re.IGNORECASE)
_FLAG_TRAILING_PAREN = re.compile(r'^(.*?)\s*\([^)]*\)\s*$')
_FLAG_SEGMENT_SPLIT  = re.compile(r'\s*(?:,|/|&|\band\b)\s*', re.IGNORECASE)

# Whole-segment thesaurus — canonical flag key -> synonym words/phrases that
# should match the ENTIRE segment (not a substring), so a musical segue like
# "Piano Intro >" or "Dark Star Intro -> Fields of Gray" is never mistaken for
# a spoken "Intro" track. Adding a synonym (Ryan, 2026-08-08: "Chatter" wasn't
# recognized as "Banter") is a one-line edit here rather than a new regex —
# _segment_pattern() below handles the optional trailing "s"/"." tolerance
# every entry already had.
_FLAG_SEGMENT_SYNONYMS = {
    'tuning':       ['tuning'],
    'banter':       ['banter', 'dialogue', 'chatter', 'crosstalk'],
    'audience':     ['audience', 'crowd'],
    'band_intros':  ['band intro', 'band introduction'],
    'introduction': ['intro', 'introduction'],
}


def _segment_pattern(words):
    alts = '|'.join(re.escape(w) for w in words)
    return re.compile(rf'^(?:{alts})s?\.?$', re.IGNORECASE)


_FLAG_SEGMENT_PATTERNS = [
    (key, _segment_pattern(words)) for key, words in _FLAG_SEGMENT_SYNONYMS.items()
]

# Whole-word/anywhere-in-segment patterns — safe as substrings because these
# words essentially never appear inside real song titles.
_FLAG_WORD_PATTERNS = [
    ('announcement', re.compile(r'\bannouncements?\b', re.IGNORECASE)),
    ('interview',    re.compile(r'\binterviews?\b',    re.IGNORECASE)),
]

# ── Songwriter credit in a trailing parenthetical ───────────────────────────
#
# Just as common a taper habit as the timestamp above: "Carry On (Stephen
# Stills)", "Ictus / Syndrome (Carla Bley)" (Ryan, 2026-08-30). Split off and
# filed as the track's songwriter rather than left sitting in the title.
#
# Deliberately narrow, on the same reasoning as _is_track_noise() and
# detect_track_flags() above: a false NEGATIVE just leaves the credit in the
# title, still visible and still fixable by hand in the wizard; a false
# POSITIVE quietly mislabels something as a songwriter, which is worse. So
# this only fires when EVERY word in the parens looks like a name (Title
# Case, letters/apostrophe/hyphen/period only, no digits) AND none of them is
# a common non-name annotation that happens to share the same shape — "(Alt
# Take)" and "(Steve Swallow)" are both two Title-Case words, and only a
# blocklist tells them apart. Reuses the same annotation vocabulary
# _FLAG_SEGMENT_SYNONYMS/_FLAG_WORD_PATTERNS already curate above, so a word
# added there for flag detection is automatically excluded here too.
_PAREN_NON_NAME_WORDS = frozenset({
    w for words in _FLAG_SEGMENT_SYNONYMS.values() for w in words
} | {
    'announcement', 'announcements', 'interview', 'interviews',
    'live', 'reprise', 'instrumental', 'acoustic', 'electric', 'unplugged',
    'outro', 'interlude', 'jam', 'improv', 'improvisation',
    'alt', 'alternate', 'version', 'take', 'edit', 'mix', 'remix',
    'excerpt', 'incomplete', 'unfinished', 'unreleased', 'demo',
    'rehearsal', 'soundcheck', 'encore', 'bonus', 'early', 'late',
    'first', 'second', 'part', 'pt', 'set', 'disc', 'side', 'cd',
    'medley', 'segue', 'false', 'start', 'fade', 'in', 'out', 'cut',
    'ending', 'beginning', 'partial', 'sbd', 'aud', 'matrix', 'fm',
})

_TRAILING_NAME_PAREN_RE = re.compile(r'\s*\(([^()]+)\)\s*$')
_NAME_WORD_RE           = re.compile(r"^[A-Z][A-Za-z'\u2019.\-]*$")


def _extract_trailing_songwriter(title):
    """
    Split a trailing "(Composer Name)" credit off a track title.

    Returns (title_without_credit, songwriter_or_None). Only pulls ONE
    composer — "(Bley/Swallow)" or "(Jagger & Richards)" style multi-writer
    credits are left in the title untouched rather than guessed at.
    """
    m = _TRAILING_NAME_PAREN_RE.search(title)
    if not m:
        return title, None
    words = m.group(1).split()
    if not (1 <= len(words) <= 3):
        return title, None
    if any(w.lower().strip('.,') in _PAREN_NON_NAME_WORDS for w in words):
        return title, None
    if not all(_NAME_WORD_RE.match(w) for w in words):
        return title, None
    base = title[:m.start()].rstrip()
    if not base:                      # title would be empty — refuse, not a credit
        return title, None
    # "Tuning (Bobby)" is a taper crediting WHO was tuning, not a song called
    # "Tuning" written by someone named Bobby — the word-shape check above
    # only looks at the parens, so a segment label left behind as the base
    # needs its own veto. Reuses the same whole-segment patterns
    # detect_track_flags() matches against ("tuning", "banter", "intro", …).
    if any(pattern.match(base) for _, pattern in _FLAG_SEGMENT_PATTERNS):
        return title, None
    return base, ' '.join(words)



def detect_track_flags(title):
    """
    Return a sorted list of suggested flag keys for a track title.
    Pure function of the title string — no DB access, safe to call from the
    ingest wizard's scan step or a one-off backfill script.
    """
    if not title:
        return []

    flags = set()
    raw = title.strip()

    if _FLAG_START_TRUNC.match(raw):
        flags.add('start_truncated')
    if _FLAG_END_TRUNC.search(raw):
        flags.add('end_truncated')
    if _FLAG_INCOMPLETE.search(raw):
        flags.add('incomplete')

    # Strip one trailing parenthetical (usually an attribution, e.g. "(Bobby)")
    # before splitting into segments, so it doesn't get treated as its own
    # segment or block a match on the segment before it.
    # One trailing parenthetical is stripped, on the theory that it is an
    # attribution ("(Bobby)") rather than the subject.
    #
    # ⚠ Unless stripping leaves NOTHING. "(Chatter)", "(Introduction)",
    # "(Announcements)", "(Cox Family Intro)" are titles that are entirely a
    # parenthetical, and the strip reduced them to "" so nothing could ever
    # match. Measured over 1,499 real track titles: this single case accounted
    # for 9 of the 53 missed non-music tracks (Ryan, 2026-08-28).
    m = _FLAG_TRAILING_PAREN.match(raw)
    base = m.group(1).strip() if m else raw
    if m and not base:
        inner = re.match(r'^\s*\((.*)\)\s*$', raw)
        base = inner.group(1).strip() if inner else raw

    for segment in _FLAG_SEGMENT_SPLIT.split(base):
        segment = segment.strip()
        if not segment:
            continue
        for key, pattern in _FLAG_SEGMENT_PATTERNS:
            if pattern.match(segment):
                flags.add(key)

    for key, pattern in _FLAG_WORD_PATTERNS:
        if pattern.search(base):
            flags.add(key)

    return sorted(flags)

# Source type keywords (scan full file text)
# ── Source detection ──────────────────────────────────────────────────────────
# ⚠ REWRITTEN 2026-08-28. The previous version was a plain substring scan over
# the whole lowercased file, first hit wins:
#
#     for kw, val in _SOURCE_KEYWORDS.items():
#         if kw in full_low: ...
#
# "aud" is a substring of audio, audiophile, inaudible, Claude and Audley.
# Measured across 44 real info files in the library: 28 were assigned a source
# and 9 of those (32%) fired on a substring inside another word. One was an
# outright misclassification — abb2001-08-03.txt was tagged AUD because the
# file contains the surname "Audley".
#
# Dict order made it worse: "aud" was tested before "mtx"/"matrix", so any
# matrix recording whose info file contains the word "audio" — which is nearly
# all of them — came out AUD. Source is the strongest single predictor of grade
# in the quality model (CV r = +0.314, see quality_scoring.py), so this fed a
# wrong answer into the score as well as the metadata.
#
# Three changes: word-boundary matching, a labelled line beats free text, and
# longest keyword first so "soundboard" is never pre-empted by a shorter token.
_SOURCE_KEYWORDS = {
    "sbd": "SBD", "soundboard": "SBD", "sound board": "SBD",
    "aud": "AUD", "audience":   "AUD",
    "mtx": "MTX", "matrix":     "MTX",
    "fm":  "FM",  "broadcast":  "FM",
}

# Longest first: "soundboard" must win over "sbd" when both appear, and
# "audience" over "aud", so the reported keyword is the specific one.
# Word boundary, but tolerant of ONE glued leading letter, because the trading
# community writes DAUD (DAT audience) and DSBD (DAT soundboard) and both were
# lost by a strict \b (measured: 3 of 44 files went from a correct answer to
# None). One letter is the whole allowance, and a trailing letter still
# disqualifies — so this admits DAUD and DSBD while still rejecting audio,
# audiophile, inaudible, Claude and Audley, which is what the strict version
# was for.
_SOURCE_PATTERNS = [
    (re.compile(r"(?<![a-z])[a-z]?" + re.escape(kw) + r"(?![a-z])", re.IGNORECASE), val)
    for kw, val in sorted(_SOURCE_KEYWORDS.items(), key=lambda kv: -len(kv[0]))
]

# A line that NAMES the source. "Source: AUD DAT master" is an assertion;
# "recorded from the audience side" is prose that happens to contain a word.
_SOURCE_LABEL_RE = re.compile(
    r"^\s*(source|src|recording\s*type|lineage)\s*[:\-]", re.IGNORECASE)


# The folder name is a SEPARATE and often better witness than the info file.
# Two conventions live side by side in a real collection:
#   library style   "... - Ancramdale, NY (AUD)"          parenthesised
#   download style  "cs2024-08-16.mtx.koucky.flac1648"    dot delimited
# quality_scoring.py::guess_source_from_name only reads the first, so it returns
# None for essentially every folder in the Download directory, which is exactly
# where triage runs.
#
# Delimiters only — no glued-prefix tolerance here. A folder name is short and
# adversarial (act names, venue names, taper names), so this stays strict.
_SOURCE_IN_FOLDER = re.compile(
    r"(?:^|[(\[._\-\s])(sbd|aud|mtx|fm)(?=$|[)\]._\-\s])", re.IGNORECASE)


def detect_source_from_name(folder_name):
    """Source marker in a folder name, or None. Last marker wins."""
    if not folder_name:
        return None
    hits = _SOURCE_IN_FOLDER.findall(folder_name)
    return hits[-1].upper() if hits else None


# ── Equipment named in a folder name (2026-09-17) ────────────────────────────
#
# Tapers put their rig in the folder name as routinely as they put the source
# in it: "gd1977-05-08.aud.schoeps.nak700.t01". detect_source_from_name()
# already harvests the SBD/AUD/MTX/FM half; this harvests the gear, which is
# lineage information and nowhere else in the metadata when the folder has no
# info file — the exact case a collector ingesting an existing library is in.
#
# Two tiers, because the false-positive problem here is not theoretical. A
# folder name is short and adversarial: it is made of act names, venue names,
# city names and taper surnames, and a wrong lineage written unattended across
# a few thousand folders is worse than no lineage at all.
#
#   Tier 1, whole words: makers whose names are not English words and not
#   plausible surnames in this position. Delimiter-bounded.
#
#   Tier 2, prefix + model number, GLUED: short tokens that are only equipment
#   when a number is stuck to them. "at" is a preposition, "ca" is California,
#   "sp" is anything at all. "at853" is a microphone. The digit run is capped
#   at four so a glued US ZIP ("ca94704") cannot read as a model.
#
# Deliberately NOT included: single-letter prefixes. "d7" and "m10" and "v3"
# are real decks, but "d1"/"d2" is the disc-number convention _parse_set_dir
# already owns (d01t01), and a lineage that swallows disc markers would be
# both wrong and confusing. Missing a deck is cheap; inventing one is not.
_GEAR_WORDS = (
    "schoeps", "neumann", "sennheiser", "nakamichi", "nak", "oktava",
    "earthworks", "josephson", "milab", "naiant", "busman", "mbho",
    "gefell", "beyerdynamic", "audio-technica", "lunatec", "nagra",
    "tascam", "marantz", "apogee", "edirol", "sony", "dat",
)

_GEAR_PREFIXES = ("akg", "at", "km", "cmc", "mk", "dpa", "ca", "sp",
                  "ua", "sbm", "pcm", "dr", "jb")

# A model number, when one is glued on: "nak700", "mbho603a", "akg451", "mk4".
# Four digits maximum so a glued US ZIP cannot read as a model.
_GEAR_MODEL = r"\d{1,4}[a-z]?"

_DELIM = r"(?:^|[(\[._\-\s>])"
_ENDIM = r"(?=$|[)\]._\-\s>])"

# Words take the model number OPTIONALLY (schoeps, nak700); prefixes REQUIRE
# it, which is the whole reason they are a separate tier -- see the note above.
_GEAR_IN_FOLDER = re.compile(
    _DELIM + "(" +
    "|".join(re.escape(w) + "(?:" + _GEAR_MODEL + ")?"
             for w in sorted(_GEAR_WORDS, key=len, reverse=True)) +
    "|" +
    "|".join(re.escape(p) + _GEAR_MODEL for p in _GEAR_PREFIXES) +
    ")" + _ENDIM,
    re.IGNORECASE,
)


def detect_gear_from_name(folder_name):
    """
    Equipment tokens named in a folder name, in the order they appear.

    Returns the matched text VERBATIM (original spelling and case), deduped
    case-insensitively. "gd77-05-08.aud.schoeps.nak700" -> ["schoeps", "nak700"].

    Returns a list rather than a lineage string on purpose. A lineage is a
    CHAIN — "Schoeps CMC6/MK4 > Lunatec V3 > SD722 > FLAC" asserts an order
    and a completeness. A folder name states neither: it names some gear. Any
    caller joining these with ">" would be fabricating a signal chain out of a
    word list, which is the uncorroborated-value failure this codebase already
    has rules about. Join with ", " and let a human make it a chain.
    """
    if not folder_name:
        return []
    out, seen = [], set()
    for hit in _GEAR_IN_FOLDER.findall(folder_name):
        key = hit.lower()
        if key not in seen:
            seen.add(key)
            out.append(hit)
    return out


def detect_source_tag_from_name(name):
    """
    Best-guess Recording.source_tag from a folder name: the first equipment
    token _GEAR_IN_FOLDER matches, verbatim (same detector as
    detect_gear_from_name, first hit only — a source TAG is one token, not
    the joined list a lineage guess would be).

    Never written without the form — see suggestions.from_info_file's
    source_tag_from_folder_name flag (spec section 5).
    """
    if not name:
        return None
    m = _GEAR_IN_FOLDER.search(name)
    return m.group(1) if m else None


# A date embedded in a folder name ("gd1988-05-01", "pat.metheny-2026-06-07")
# — used only to keep detect_shnid_from_name() from mistaking the date's own
# year for an LMA/etree shnid. Deliberately narrow (4-digit year, delimited
# month/day): the goal is recognising a date that's THERE, not parsing every
# date shape a folder name might use.
_DATE_IN_NAME_RE = re.compile(r"((?:19|20)\d{2})[._-]\d{1,2}[._-]\d{1,2}")

# Full YYYY-MM-DD (or ./_ delimited) date inside a folder name -- the single
# canonical source for this pattern (Ingest Field Resolver spec v1, section
# 10 build order). Previously duplicated between app/api/ingest.py and
# app/utils/bulk_ingest.py; both now import this one instead.
FOLDER_DATE_RE = re.compile(
    r'\b(19|20)\d{2}[-._](0[1-9]|1[0-2])[-._](0[1-9]|[12]\d|3[01])\b')

# 3 to 7 all-digit characters, delimited by . _ or - (or string start/end) on
# both sides — never glued to letters ("flac16" must not match).
_SHNID_SEGMENT_RE = re.compile(r"(?:^|[._-])(\d{3,7})(?:[._-]|$)")


def detect_shnid_from_name(name):
    """
    Best-guess Recording.etree_shnid (the LMA/etree source id) from a folder
    name: the LAST dot-, underscore- or hyphen-delimited all-digit segment of
    3 to 7 digits that is not the year of a matched date and not itself in
    1900..2099 (a plausible year even without a full date match).

    'gd1988-05-01.ec7.bowen.foster.118671.flac16' -> 118671 ("flac16" is
    glued to letters, not a segment, so it never competes).
    'gd1969-01-25.sbd.kaplan.7923.sbeok.shnf' -> 7923.
    'pat.metheny-2026-06-07_24.96_tr.16' -> None (every digit run left after
    excluding the date's year and other 1900..2099 look-alikes is 2 digits).

    Never written without the form — see suggestions.from_info_file's
    shnid_from_folder_name flag (spec section 5).
    """
    if not name:
        return None
    date_year = None
    dm = _DATE_IN_NAME_RE.search(name)
    if dm:
        date_year = dm.group(1)
    candidates = []
    for seg in _SHNID_SEGMENT_RE.finditer(name):
        digits = seg.group(1)
        if digits == date_year:
            continue
        if 1900 <= int(digits) <= 2099:
            continue
        candidates.append(int(digits))
    return candidates[-1] if candidates else None


def detect_source(text):
    """
    Best guess at the recording source (SBD / AUD / MTX / FM), or None.

    Labelled lines are searched first and in file order, because a taper who
    wrote "Source: SBD" has told us the answer directly. Only if no labelled
    line yields anything does this fall back to the whole text, where a bare
    keyword is a hint rather than a statement.

    Word boundaries throughout — see the note on _SOURCE_KEYWORDS for what the
    substring version did to 32% of the corpus.
    """
    if not text:
        return None

    labelled = [ln for ln in text.splitlines() if _SOURCE_LABEL_RE.match(ln)]
    for line in labelled:
        for pat, val in _SOURCE_PATTERNS:
            if pat.search(line):
                return val

    for pat, val in _SOURCE_PATTERNS:
        if pat.search(text):
            return val
    return None

# Lineage section triggers — explicit labels only (bare ">" removed to avoid false positives)
_LINEAGE_LABELS = {"lineage", "source:", "transfer", "recording info", "recorded by", "chain:"}

# Venue keywords
_VENUE_WORDS = {
    "theater","theatre","stadium","arena","festival","amphitheater",
    "hall","halle","saal","kursaal",
    "concert","club","studio","radio","pavilion","auditorium","center","centre",
    "ballroom","opera","university","college","fillmore","ryman","birchmere",
    "inn","stage","coffeehouse","tent","café","cafe","lounge","saloon",
    "fairground","garden","park","ranch","farm","museum","coliseum",
    "field","court","bowl","forum","palace","pier","warehouse","dome","barn",
}


# ── Private helpers ────────────────────────────────────────────────────────────

def _is_filename_line(line):
    """Detect identifier lines like 'BillEvans.1980-02-22.ECM260F' — no spaces, has dots."""
    return "." in line and " " not in line.strip()


def _looks_like_date_line(line):
    """Quick check: does this line likely contain a date?"""
    low = line.lower()
    if re.search(r"\b(19|20)\d{2}\b", line):
        return True
    if re.search(r"\b\d{1,2}[-./]\d{1,2}[-./]\d{2,4}\b", line):
        return True
    if any(m in low.split() for m in _MONTH_NAMES):
        return True
    return False


def _parse_date(line):
    """
    Extract a date from a block of header text with the strict grammar.
    Returns (year, month, day, raw_str) or None. A component the text did not
    write is None -- the old dateutil fuzzy path filled it from today's date
    (2026-10-03). raw_str is the line the leading mention sits on.
    """
    best, _ = _best_show_date(line)
    if not best or best.year is None:
        return None
    s = line.rfind("\n", 0, best.span[0]) + 1
    e = line.find("\n", best.span[1])
    raw = line[s:len(line) if e < 0 else e].strip()
    return best.year, best.month, best.day, raw


def _parse_location_plain(line):
    """
    Extract (city, state, country) from a location line, positionally.

    Recognises the country and/or US state from the END of the line, then the
    remaining last comma-part is the city (any earlier parts are venue text and
    are ignored). Handles:
        "New York, NY"                  -> ("New York", "NY", "US")
        "New York, NY, USA"             -> ("New York", "NY", "US")
        "Fillmore East, New York, NY"   -> ("New York", "NY", "US")   (drops venue)
        "Osaka, Japan"                  -> ("Osaka", None, "Japan")
        "Ann Arbor MI"                  -> ("Ann Arbor", "MI", "US")   (no comma)
    Returns (None, None, None) when no state/country is recognised — i.e. the
    line is not a location. City is NOT validated against the gazetteer, so
    multi-word cities are never truncated (the old "New York"->"York" bug).
    """
    line = line.strip()
    if not line:
        return None, None, None

    parts = [p.strip() for p in line.split(",") if p.strip()]

    # No comma but "City ST" / "City Country" — peel the trailing region token.
    if len(parts) == 1 and " " in parts[0]:
        head, tail = parts[0].rsplit(" ", 1)
        if (tail.upper() in _US_STATE_CODES or tail.lower() in _US_STATE_NAMES
                or tail.lower() in _COUNTRY_NAMES or tail.lower() in _COUNTRY_ALIASES):
            parts = [head.strip(), tail.strip()]

    state = country = None

    # Country from the last part (known alias, or a gazetteer country name).
    if parts:
        ll = parts[-1].lower()
        if ll in _COUNTRY_ALIASES:
            country = _COUNTRY_ALIASES[ll]; parts.pop()
        elif ll in _COUNTRY_NAMES:
            country = parts[-1].title(); parts.pop()

    # US state from the (new) last part — 2-letter code or full name.
    if parts:
        last = parts[-1]
        if last.upper() in _US_STATE_CODES:
            state, country = last.upper(), "US"; parts.pop()
        elif last.lower() in _US_STATE_NAMES:
            state, country = _US_STATE_NAMES[last.lower()], "US"; parts.pop()

    # Not a location line unless we recognised a state or country.
    if state is None and country is None:
        return None, None, None

    city = parts[-1].title() if parts else None
    return city, state, country


# ── Venue/location dash-split (parser fix, spec section 7) ─────────────────
#
# A taper's header line often reads "Venue \u2013 City, State, Country" (en
# dash, em dash, or a plain hyphen with spaces on both sides). Read
# positionally, the whole thing looked like ONE location line and the venue
# text ended up glued onto the city ("Stars \u2013 Philadelphia" as the
# "city"). Splitting first means both halves get parsed for what they are:
# the venue by _extract_venue(), the place by _parse_location() below.
_DASH_SPLIT_RE = re.compile(r"\s+[\u2013\u2014-]\s+")


def _split_dash_location(line):
    """
    "Venue \u2013 City, State, Country" -> ("Venue", "City, State, Country")
    when the right side reads as a location and the left side does not
    (so a plain "City - State" line is never mistaken for one). Returns
    (None, line) when the line doesn't split this way.
    """
    parts = _DASH_SPLIT_RE.split(line, maxsplit=1)
    if len(parts) != 2:
        return None, line
    left, right = parts[0].strip(), parts[1].strip()
    if not left or not right:
        return None, line
    if any(_parse_location_plain(left)):
        return None, line
    if not any(_parse_location_plain(right)):
        return None, line
    return left, right


def _parse_location(line):
    """Dash-aware wrapper: splits a "Venue - City, State" line before
    parsing, so the location half is never contaminated by the venue text
    sitting in front of it. See _split_dash_location()."""
    _, loc_part = _split_dash_location(line)
    return _parse_location_plain(loc_part)


# Venue plausibility (spec section 7): a candidate is rejected -- treated as
# no venue, not ingested as one -- when it's obviously not a place name. This
# sends the folder to review rather than inventing a venue out of a line like
# "Two Shows: Show 1: 9:00 p.m. and Show 2: 11:00 p.m."
_VENUE_CLOCK_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
_VENUE_IMPLAUSIBLE_START_RE = re.compile(
    r"^(show\s+\d|two\s+shows|set\s+\d)", re.IGNORECASE)


def venue_plausible(candidate):
    """True if `candidate` could plausibly be a venue name."""
    c = (candidate or "").strip()
    if not c:
        return False
    if _VENUE_CLOCK_TIME_RE.search(c):
        return False
    if _VENUE_IMPLAUSIBLE_START_RE.match(c):
        return False
    return True


def _extract_venue(header_lines):
    """
    Two-pass venue extraction:
      1. Positional: first line after artist that survives all filters (most reliable)
      2. Keyword scan on date/location lines we skipped (catches embedded venues like
         "1-28-89 Birchmere, Alexandria, VA")
    """
    skipped_date_loc = []   # date/location lines saved for keyword fallback

    # Pass 1 — positional
    for line in header_lines[1:]:
        low = line.lower()
        is_date = _looks_like_date_line(line)

        # "Venue - City, State, Country" (spec section 7): recognise and
        # peel the venue off BEFORE the line gets treated as a plain
        # location and swallowed whole (Stars – Philadelphia would
        # otherwise read as a location whose "city" is "Stars – Philadelphia").
        dash_venue, _dash_loc = _split_dash_location(line)
        if dash_venue and not is_date:
            if venue_plausible(dash_venue):
                return dash_venue.strip()
            # The split held but the venue half didn't pass plausibility
            # (e.g. a clock time snuck in) -- treat the line as a location
            # line only, same as any other, and keep scanning.
            skipped_date_loc.append(line)
            continue

        # Save date and location lines for keyword fallback, but skip them here
        city, state, country = _parse_location(line)
        is_location = bool(city or state or country)
        if is_date or is_location:
            skipped_date_loc.append(line)
            continue

        if any(lbl in low for lbl in _LINEAGE_LABELS):
            continue
        # Skip lines that start with a source keyword (e.g. "SBD (analog 4th gen...)")
        first_word = low.split()[0] if low.split() else ""
        if first_word in _SOURCE_KEYWORDS:
            continue
        # Skip band-member lines: "Firstname Lastname - instrument"
        if re.match(r"^[A-Z][a-z]+\s+[A-Z][a-z].*\s[-:]\s+[a-z]", line):
            continue
        # Skip short all-caps section labels (SETLIST, NOTES, etc.)
        if line.isupper() and len(line.split()) <= 2:
            continue
        # Skip numbered/ordinal event lines: "27. Internationale Jazzwoche", "3rd Jazz Festival"
        if re.match(r"^\d+(st|nd|rd|th)?\s*[.\s]", line, re.IGNORECASE):
            continue

        cand = line.strip()
        if venue_plausible(cand):
            return cand
        # Implausible (e.g. "Two Shows: Show 1: 9:00 p.m. ..."): not a venue,
        # not a location either -- just skip it and keep looking.
        continue

    # Pass 2 — keyword scan on skipped date/location lines
    for line in skipped_date_loc:
        low = line.lower()
        if any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in _VENUE_WORDS):
            segments = re.split(r",\s*|@\s*", line)
            for seg in segments:
                if any(re.search(r"\b" + re.escape(w) + r"\b", seg.lower()) for w in _VENUE_WORDS):
                    # Strip any leading date token (e.g. "1-28-89 Birchmere")
                    seg = re.sub(r"^\d{1,2}[-./]\d{1,2}[-./]\d{2,4}\s*", "", seg).strip()
                    if seg and venue_plausible(seg):
                        return seg

    return None


def _fuzzy_match(candidate, known_names, cutoff=0.85):
    """Return the best match from known_names above cutoff, or None."""
    if not known_names:
        return None
    norm       = candidate.title()
    norm_known = [n.title() for n in known_names]
    matches    = get_close_matches(norm, norm_known, n=1, cutoff=cutoff)
    if matches:
        return known_names[norm_known.index(matches[0])]
    return None


def _is_track_noise(title):
    """Return True if a matched track 'title' is actually noise — hash, filename, date fragment."""
    low = title.lower()
    if ".flac" in low:
        return True
    if re.search(r"\bflac\b", low):                           # audio format spec line
        return True
    if re.search(r"\bkhz\b", low):                            # audio spec line
        return True
    if re.match(r"^[\da-f]{8,}", low):                        # hex checksum
        return True
    if re.match(r"^\d{1,2}[-./]\d{4}$", title):              # date fragment "19-1978"
        return True
    if re.match(r"^\d{1,2}[-./]\d{1,2}", title) and len(title) < 15:  # short date range "6-15.1978"
        return True
    if _TOTAL_DURATION_RE.match(title):                        # title is itself a bare duration, e.g. "46:28"
        return True
    return False


def _read_text_auto(file_path):
    """
    Read a text file and return a clean unicode string regardless of encoding.
    Handles UTF-16 LE/BE (with BOM), UTF-8 BOM, and plain UTF-8/Latin-1.
    """
    with open(file_path, "rb") as fh:
        raw_bytes = fh.read()

    # Detect BOM and decode accordingly
    if raw_bytes.startswith(b"\xff\xfe"):          # UTF-16 LE BOM
        return raw_bytes.decode("utf-16-le", errors="replace").lstrip("﻿")
    if raw_bytes.startswith(b"\xfe\xff"):          # UTF-16 BE BOM
        return raw_bytes.decode("utf-16-be", errors="replace").lstrip("﻿")
    if raw_bytes.startswith(b"\xef\xbb\xbf"):      # UTF-8 BOM
        return raw_bytes[3:].decode("utf-8", errors="replace")

    # No BOM — try UTF-8, then Windows-1252 (covers ASCII, Latin-1, and CP1252 curly quotes etc.)
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return raw_bytes.decode("cp1252", errors="replace")


def parse_info_file(file_path, known_artists=None, known_venues=None, text=None):
    """
    Parse a ROIO info/text file and extract structured metadata suggestions.

    Args:
        file_path:      path to .txt info file. May be None when `text` is given.
        known_artists:  list of artist name strings for fuzzy matching (optional)
        known_venues:   list of venue name strings for fuzzy matching (optional)
        text:           parse THIS string instead of reading `file_path`
                        (2026-09-01, for the Rescan button on Add Recording).

    `text` exists so a rescan can re-run the inference over the reviewer's
    EDITED info file without first writing it to their disk. The alternative
    was save-then-rescan, which turns a read-only "try again" into a silent
    modification of the collector's source folder — and the info file is the
    taper's own words, which this app is careful not to touch by accident.

    An empty string is a legitimate value here and must not fall back to the
    file: a reviewer who cleared the box means the file is empty. Hence the
    `is not None` test rather than a truthiness one.

    Returns dict:
        raw_content, artist, artist_match, year, month, day, date_str,
        venue, venue_match, city, state, country, source, lineage,
        tracks [ {number, title} ]
    """
    if text is not None:
        raw = text
    else:
        try:
            raw = _read_text_auto(file_path)
        except OSError:
            return {"raw_content": "", "tracks": []}

    lines = raw.splitlines()

    # ── Pass 1: split into header block and track block ───────────────────────
    header_lines = []
    track_pairs  = []       # [(number, title, songwriter), ...]
    in_tracks    = False
    tracks_ended = False    # set once a trailing Notes/Comments/etc. heading is seen
    disc_offset  = 0        # running offset so multi-disc restarts (1, 2, 3... 1, 2, 3...)
    last_raw_num = None     # come out sequential instead of colliding by number

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if tracks_ended:
            continue

        if in_tracks and _TRACKLIST_END_RE.match(stripped):
            tracks_ended = True
            continue

        # Bare duration line ("1:46:28") — never a track, whether it's
        # sitting in the header or turns up again as a footer total.
        if _TOTAL_DURATION_RE.match(stripped):
            if not in_tracks:
                header_lines.append(stripped)
            continue

        m = _TRACK_PATTERN.match(stripped)
        if m:
            num   = int(m.group(1))
            raw_title = _TRAILING_TS_RE.sub('', m.group(2).strip())
            raw_title = _TRAILING_PAREN_TS_RE.sub('', raw_title)
            # Split off a trailing "(Composer Name)" credit BEFORE title-
            # casing — a name should keep its own capitalisation, not run
            # through the "on"/"of"/"in"-stay-lowercase rule meant for
            # titles (Ryan, 2026-08-30).
            raw_title, songwriter = _extract_trailing_songwriter(raw_title)
            title = title_case(raw_title)
            if not _is_track_noise(title) and (in_tracks or len(header_lines) >= 2):
                in_tracks = True
                # Multi-disc listings restart numbering at 1 each disc — e.g.
                # "*** Disc Two ***" followed by "1. Song". Detect the restart
                # (this number <= the last one seen) and carry a running
                # offset so the combined list comes out sequential (Ryan,
                # 2026-07-16: "23 tracks... just split out by disc, the
                # numbering restarts").
                if last_raw_num is not None and num <= last_raw_num:
                    disc_offset += last_raw_num
                last_raw_num = num
                track_pairs.append((disc_offset + num, title, songwriter))
                continue

        if not in_tracks:
            header_lines.append(stripped)

    # ── Pass 2: extract fields from header ────────────────────────────────────
    result = {
        "raw_content":  raw,
        "artist":       None,
        "artist_match": None,
        "year":         None,
        "month":        None,
        "day":          None,
        "date_str":     None,
        "venue":        None,
        "venue_match":  None,
        "city":         None,
        "state":        None,
        "country":      None,
        "source":       None,
        "lineage":      None,
        "tracks":       [],
    }

    # Artist — first non-blank, non-filename line in the first 3 lines
    for line in header_lines[:3]:
        if not _is_filename_line(line) and not _looks_like_date_line(line):
            result["artist"]       = title_case(line)
            result["artist_match"] = _fuzzy_match(line, known_artists or [])
            break

    # Venue — keyword scan then positional fallback
    venue_raw = _extract_venue(header_lines)
    if venue_raw:
        result["venue"]       = title_case(venue_raw)
        result["venue_match"] = _fuzzy_match(venue_raw, known_venues or [])

    # City / State / Country — first header line that validates
    for line in header_lines:
        city, state, country = _parse_location(line)
        if city or state or country:
            result["city"]    = city
            result["state"]   = state
            result["country"] = country
            break

    # Date — every mention in the header is read together (strict grammar, no
    # defaults).
    parsed = _parse_date("\n".join(header_lines))
    if parsed:
        result["year"], result["month"], result["day"], result["date_str"] = parsed

    # Source type — labelled lines first, then the whole file. See detect_source.
    result["source"] = detect_source(raw)

    # Lineage — collect the contiguous block of non-blank lines starting at an
    # explicit lineage label, stopping at the next blank line (or a hard line
    # cap). This is lower-priority than the core fields — it should only fire
    # when it's confidently bounded to a real chain description, not guess at
    # where one ends. Info files routinely have unrelated sections (setlist,
    # taper notes, footnotes) after the label; without a stop condition this
    # used to run to EOF and swallow the whole rest of the file.
    _MAX_LINEAGE_LINES = 8
    lineage_buf = []
    for i, line in enumerate(lines):
        low = line.strip().lower()
        if any(lbl in low for lbl in _LINEAGE_LABELS):
            lineage_buf.append(line.strip())
            for follow in lines[i + 1:]:
                if not follow.strip() or len(lineage_buf) >= _MAX_LINEAGE_LINES:
                    break
                lineage_buf.append(follow.strip())
            break
    if lineage_buf:
        result["lineage"] = " ".join(lineage_buf)

    # Tracks
    result["tracks"] = [{"number": n, "title": t, "songwriter": sw} for n, t, sw in track_pairs]

    return result


def _titlecase(s):
    """Simple title-case that preserves all-caps abbreviations (SBD, AUD, etc.)."""
    words = s.split()
    out   = []
    for w in words:
        if w.upper() == w and len(w) > 1:
            out.append(w)
        else:
            out.append(w.capitalize())
    return " ".join(out)


def build_scan_payload(folder_path, info_override=None):
    """
    Non-destructive scan of a source folder — the single shared foundation for
    every "what's in this folder" question in the app: the Add Recording scan
    step (POST /api/recordings/scan) AND batch import (POST /api/ingest/batch-scan)
    both build their metadata suggestions and health score from this, so a
    folder scores identically no matter which flow scanned it.

    Returns the full scan payload (audio files, parsed tag/info-file
    suggestions, fingerprints, and a compute_health() score), or None if the
    folder has no audio files.

    Logs a "step" checkpoint (see utils/debug_log.py) after each phase so a
    slow/stuck scan is visible in the debug panel's Live Server Activity
    section WHILE it's still running, keyed to this folder's path — this is
    the shared foundation for both the interactive Review scan and batch
    import, so instrumenting it here covers both for free.

    `info_override` (2026-09-01) is {"filename": str|None, "content": str} and
    means "parse THIS text for that candidate instead of the bytes on disk".
    It exists for Add Recording's Rescan button, which re-runs the inference
    over an info file the reviewer has edited but not necessarily saved.
    Defaults to None, in which case every code path below is byte-identical to
    what it was — batch import passes nothing and is unaffected.
    """
    from app.utils.debug_log import log_step
    job = f"scan:{folder_path}"
    log_step(job, "start", "walking folder (os.listdir/os.walk — this is where a slow "
                           "NAS mount or a huge folder shows up as a long gap before the next step)")

    files = scan_folder(folder_path)
    log_step(job, "walked folder",
             f"{len(files['audio_files'])} audio · {len(files['text_files'])} text · "
             f"{len(files['fingerprints'])} fingerprint file(s)")
    if not files["audio_files"]:
        log_step(job, "done", "no audio files found")
        return None

    from_tags = read_flac_tags(files["audio_files"])
    log_step(job, "read FLAC tags", f"{len(files['audio_files'])} file(s)")

    # Parse the LOCATION tag into city/state/country using the same
    # geonamescache-backed parser as the info file (best-effort, graceful fallback)
    tag_city = tag_state = tag_country = None
    tag_location = from_tags["container"].get("location") or ""
    if tag_location:
        try:
            tag_city, tag_state, tag_country = _parse_location(tag_location)
        except Exception:
            pass

    # Parse ALL text file candidates (scored/sorted best-first by scan_folder).
    from_info         = {}
    info_file_content = None
    parsed_candidates = []
    override_name = (info_override or {}).get("filename")
    override_used = False
    for tf in files["text_files"]:
        # Only the named candidate is overridden. Applying the text to every
        # candidate would make the file switcher show one file's contents under
        # every filename.
        use_text = None
        if info_override and (override_name is None or override_name == tf["filename"]):
            use_text = info_override.get("content") or ""
            override_used = True
        parsed = parse_info_file(tf["path"], text=use_text)
        log_step(job, "parsed info file",
                 tf["filename"] + (" (edited text)" if use_text is not None else ""))
        entry  = {
            "filename":    tf["filename"],
            "score":       tf.get("score", 0),
            "content":     parsed.get("raw_content", ""),
            "suggestions": {
                "artist":       parsed.get("artist"),
                "artist_match": parsed.get("artist_match"),
                "year":         parsed.get("year"),
                "month":        parsed.get("month"),
                "day":          parsed.get("day"),
                "venue":        parsed.get("venue"),
                "venue_match":  parsed.get("venue_match"),
                "city":         parsed.get("city"),
                "state":        parsed.get("state"),
                "country":      parsed.get("country"),
                "source":       parsed.get("source"),
                "lineage":      parsed.get("lineage"),
                "tracks": [
                    {"number": t["number"], "title": t["title"],
                     "songwriter": t.get("songwriter")}
                    for t in parsed.get("tracks", [])
                ],
            },
        }
        parsed_candidates.append(entry)
    # A folder with NO text file at all, where the reviewer typed one from
    # scratch: there is no candidate to override, so the typed text becomes one.
    # Without this, Rescan on such a folder would silently ignore everything
    # they wrote — the failure looking exactly like "the parser found nothing",
    # which is a different and much more misleading answer.
    if info_override and not override_used and (info_override.get("content") or "").strip():
        content = info_override["content"]
        parsed  = parse_info_file(None, text=content)
        parsed_candidates.insert(0, {
            "filename": override_name or "info.txt",
            "score": 0,
            "content": parsed.get("raw_content", ""),
            "suggestions": {
                "artist":       parsed.get("artist"),
                "artist_match": parsed.get("artist_match"),
                "year":         parsed.get("year"),
                "month":        parsed.get("month"),
                "day":          parsed.get("day"),
                "venue":        parsed.get("venue"),
                "venue_match":  parsed.get("venue_match"),
                "city":         parsed.get("city"),
                "state":        parsed.get("state"),
                "country":      parsed.get("country"),
                "source":       parsed.get("source"),
                "lineage":      parsed.get("lineage"),
                "tracks": [
                    {"number": t["number"], "title": t["title"],
                     "songwriter": t.get("songwriter")}
                    for t in parsed.get("tracks", [])
                ],
            },
        })

    if parsed_candidates:
        from_info         = parsed_candidates[0]["suggestions"]
        info_file_content = parsed_candidates[0]["content"]

    # Read fingerprint file contents
    fingerprints = []
    for fp in files["fingerprints"]:
        try:
            with open(fp["path"], "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            content = None
        fingerprints.append({
            "type":     fp["type"],
            "filename": fp["filename"],
            "rel_path": fp.get("rel_path", fp["filename"]),
            "content":  content,
        })
    if files["fingerprints"]:
        log_step(job, "read fingerprint files", f"{len(files['fingerprints'])} file(s)")

    resp = {
        "folder_path":      folder_path,
        "folder_name":      os.path.basename(folder_path),
        "audio_file_count": len(files["audio_files"]),
        "sets_detected":    files.get("sets_detected", False),
        "audio_files": [
            {
                "index":    f["index"],
                "filename": f["filename"],
                "rel_path": f.get("rel_path", f["filename"]),
                "set_number": f.get("set_number"),
                "disc_number": f.get("disc_number"),
                "disc_track_number": f.get("disc_track_number"),
            }
            for f in files["audio_files"]
        ],
        "info_file_content": info_file_content,
        "text_file_candidates": parsed_candidates,
        "fingerprints":      fingerprints,
        "suggestions": {
            "from_tags": {
                "artist":       from_tags["container"].get("artist"),
                "concert_date": from_tags["container"].get("concert_date"),
                "venue":        from_tags["container"].get("venue"),
                "location":     from_tags["container"].get("location"),
                "city":         tag_city,
                "state":        tag_state,
                "country":      tag_country,
                "source":       from_tags["container"].get("source"),
                "lineage":      from_tags["container"].get("lineage"),
                "tracks": [
                    {
                        "index":        t["index"],
                        "filename":     t["filename"],
                        "rel_path":     t.get("rel_path", t["filename"]),
                        "track_number": t["track_number"],
                        "title":        t["title"],
                        "duration":     t["duration"],
                        # None (not {}) means read_flac_tags could not open
                        # this file at all -- distinct from an opened file
                        # with no tags, which is {} (R2-N3). Do not default
                        # this away; bulk_ingest.py's extract() relies on the
                        # distinction to spot an all-corrupt folder without
                        # opening every file a second time.
                        "raw":          t.get("raw"),
                        "open_error":   t.get("open_error"),
                    }
                    for t in from_tags["tracks"]
                ],
            },
            "from_info_file": {
                "artist":       from_info.get("artist"),
                "artist_match": from_info.get("artist_match"),
                "year":         from_info.get("year"),
                "month":        from_info.get("month"),
                "day":          from_info.get("day"),
                "venue":        from_info.get("venue"),
                "venue_match":  from_info.get("venue_match"),
                "city":         from_info.get("city"),
                "state":        from_info.get("state"),
                "country":      from_info.get("country"),
                "source":       from_info.get("source"),
                "lineage":      from_info.get("lineage"),
                "source_tag":   from_info.get("source_tag"),
                "etree_shnid":  from_info.get("etree_shnid"),
                "tracks": [
                    {"number": t["number"], "title": t["title"],
                     "songwriter": t.get("songwriter")}
                    for t in from_info.get("tracks", [])
                ],
            },
        },
    }
    # Source, last resort: the folder's own name. Measured over 39 recordings
    # whose folder states a source, the info file alone answered 26 of them;
    # adding the folder name answers all 39. Applied only where BOTH the tag
    # and the info file are silent, so an explicit statement always wins over
    # a name. See detect_source_from_name.
    if not resp["suggestions"]["from_tags"].get("source") \
            and not resp["suggestions"]["from_info_file"].get("source"):
        from_folder = detect_source_from_name(resp["folder_name"])
        if from_folder:
            resp["suggestions"]["from_info_file"]["source"] = from_folder
            resp["source_from_folder_name"] = True

    # Lineage, same last-resort rule and the same reason (2026-09-17). A taper
    # who writes "gd1977-05-08.aud.schoeps.nak700" has named their rig, and for
    # a folder with no info file that is the ONLY place the rig is recorded —
    # which is exactly the folder a collector ingesting an existing library is
    # looking at.
    #
    # Joined with ", " and never ">": the folder named some gear, it did not
    # state a signal chain. Writing "schoeps > nak700" would assert an order
    # and a completeness nothing here observed, and a reviewer reading it back
    # would have no way to tell the invented arrow from a taper's own.
    if not resp["suggestions"]["from_tags"].get("lineage") \
            and not resp["suggestions"]["from_info_file"].get("lineage"):
        gear = detect_gear_from_name(resp["folder_name"])
        if gear:
            resp["suggestions"]["from_info_file"]["lineage"] = ", ".join(gear)
            resp["lineage_from_folder_name"] = True

    # Source tag and shnid (spec section 5): folder-name only — neither the
    # FLAC tags nor the info file parser produces these, so there is no
    # "explicit statement wins" guard to apply, and the suggestion is never
    # written to the Recording without the form (Add Recording / View
    # Recording's inline edit).
    source_tag = detect_source_tag_from_name(resp["folder_name"])
    if source_tag:
        resp["suggestions"]["from_info_file"]["source_tag"] = source_tag
        resp["source_tag_from_folder_name"] = True

    shnid = detect_shnid_from_name(resp["folder_name"])
    if shnid:
        resp["suggestions"]["from_info_file"]["etree_shnid"] = shnid
        resp["shnid_from_folder_name"] = True

    resp["health"] = compute_health(resp)
    log_step(job, "done", f"health {resp['health']['score']} ({resp['health']['band']})")

    # Resolved fields + verdict (Ingest Field Resolver spec v1, chunk 6):
    # every consumer of build_scan_payload() gets the resolver's answer for
    # free, so the wizard's prefill and Review & Ingest never need their own
    # copy of these merge rules. Best-effort -- library_root/placement come
    # from app context, which some pure callers of this function (tests,
    # scripts) don't have, and a scan payload must still be usable without
    # them; resolved is simply None in that case rather than failing the scan.
    try:
        from app.utils.resolve import resolve as _resolve_scan
        _library_root = None
        _placement = None
        try:
            from flask import current_app as _current_app
            _library_root = _current_app.config.get("LIBRARY_ROOT")
        except Exception:
            pass
        try:
            from app.utils import node_settings as _node_settings
            _placement = _node_settings.get_file_handling().get("placement")
        except Exception:
            pass
        resp["resolved"] = _resolve_scan(
            resp, library_root=_library_root, placement=_placement).to_dict()
    except Exception as e:  # noqa: BLE001
        # Missing app context is already absorbed by the two inner guards and
        # resolve() itself is pure, so reaching here means a real resolver bug.
        # Log it: a silent None renders as an empty prefill with no trace.
        import traceback as _tb_res
        _tb_res.print_exc()
        log_step(job, "resolver failed", f"{type(e).__name__}: {e}")
        resp["resolved"] = None

    return resp


# ── File system operations ─────────────────────────────────────────────────────

def _undo_transfer(moved, dest_folder):
    """
    Put things back after a cancelled `move_to_library`.

    Every file already relocated goes back to where it came from — otherwise
    cancelling would silently scatter a show across two directories.

    Best-effort throughout: a failure to clean up must not mask the
    cancellation itself, which is what the user actually asked for.
    """
    for original, target in reversed(moved):
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(original))
        except OSError:
            pass
    try:
        shutil.rmtree(str(dest_folder), ignore_errors=True)
    except OSError:
        pass


class IngestCancelled(Exception):
    """
    Raised when a user cancels an in-flight ingest.

    Not an error: `move_to_library` has already undone its own filesystem work
    by the time this propagates, and the caller only needs to roll back the DB
    session (which has not committed yet — `_do_confirm` flushes throughout and
    commits exactly once, at the very end).
    """


def resolve_ingest_file_path(orig_rel_path, audio_rename_map, flatten):
    """
    Where a single audio file lands, relative to the destination folder's
    root, for a given ingest. Shared between move_to_library() (which
    actually writes the file there) and app.api.ingest._do_confirm's Track
    creation step (which stores the same path as Track.file_path), so the
    two can never drift apart (spec section 3.1, D1).

    flatten=True: the file lands at the destination root under its (possibly
    renamed) basename, regardless of how deeply nested it was in the source
    (CD1/, Disc 2/, ...) -- this is what fixes multi-disc sources whose
    per-disc filenames collide once flattened.

    flatten=False: the file keeps its original subdir prefix (spec: "names
    and nesting preserved" when rename_files is off); only the basename is
    substituted from audio_rename_map, so a renaming scheme without a
    position token (spec section 4's flattens()) can still be applied
    without disturbing the source's own folder structure.
    """
    orig_rel_path = orig_rel_path or ""
    basename = os.path.basename(orig_rel_path)
    new_name = (audio_rename_map or {}).get(orig_rel_path) \
        or (audio_rename_map or {}).get(basename) \
        or basename
    if flatten:
        return new_name
    parent = os.path.dirname(orig_rel_path).replace(os.sep, "/")
    return f"{parent}/{new_name}" if parent else new_name


def move_to_library(source_folder, library_root, artist_name, folder_name,
                    progress_cb=None, audio_rename_map=None,
                    cancel_cb=None, under_artist_folder=True, flatten=True):
    """
    Move a source folder into the library. A source outside LIBRARY_ROOT is
    always moved; there is no copy behavior.

    Audio files are renamed per `audio_rename_map` (original rel_path → new
    filename) and, when `flatten` is true, moved into the destination
    folder's ROOT regardless of how deeply nested they were in the source
    (CD1/, Disc 2/, flac/, ...) — this keeps Track.file_path free of subdir
    prefixes and guarantees continuous, collision-free filenames even when a
    multi-disc source reset filenames independently per disc (the CD1/CD2
    bug this replaced — 2026-07-14). When `flatten` is false, an audio file
    keeps its original subdir prefix too (spec section 1.1: "Off: names and
    nesting preserved" under rename_files) — see resolve_ingest_file_path(),
    the one place this decision is made, shared with the caller's Track.file_path.
    Non-audio content (art, text files, etc.) always keeps its original
    relative structure under dest_folder.

    Renaming does not affect fingerprint verification: FFP/MD5/ST5 are
    content hashes, independent of filename. Fingerprint-file matching is
    done by the caller against ORIGINAL filenames (before this rename) —
    see compute_audio_rename_map() and app.api.ingest._do_confirm.

    Args:
        source_folder     : str  — absolute path to source folder
        library_root       : str  — LIBRARY_ROOT from config
        artist_name         : str  — canonical artist name (used as subdirectory)
        folder_name        : str  — canonical folder name from build_folder_name().
                              May be disambiguated with a "(2)"-style suffix if a
                              folder by this name already exists under the artist
                              directory — see unique_folder_name(). The caller should
                              re-read the actual name from the returned path rather
                              than assume this argument is what landed on disk.
        under_artist_folder: bool — whether to file the show under an
                              <artist_name>/ directory (the default, and what
                              Trellis-created libraries look like) or flat at
                              the library root. A collector who pointed Trellis
                              at their OWN library may keep every show in one
                              flat level with the artist in the folder name;
                              filing new material under an artist directory
                              there leaves them with a permanently hybrid tree.
                              An INSTALL-level setting, not a per-user one —
                              a library has one shape, so it is read from
                              node_setting and passed in by the caller rather
                              than looked up here (this function has no app
                              context and its tests call it directly).
        progress_cb         : callable(copied_bytes, total_bytes) | None — progress
        audio_rename_map   : {rel_path_or_basename: new_filename}, from
                              compute_audio_rename_map(). An audio file with
                              no entry keeps its original basename, still
                              flattened to dest_folder's root.
        cancel_cb          : callable() -> bool | None — polled BETWEEN files.
                              When it returns True this function undoes
                              everything it has done so far and raises
                              IngestCancelled.
        flatten            : bool — whether audio files land at dest_folder's
                              root (True, the default — matches every caller
                              before this parameter existed) or keep their
                              original subdir prefix (False). See
                              resolve_ingest_file_path().

    In-root bulk_ingest (spec section 1.1, Bulk Ingest chunk 1, 2026-09-26,
    supersedes the 2026-09-25 S1/R2 notes above): a source folder whose
    realpath already sits inside LIBRARY_ROOT is never moved, renamed,
    flattened or deduped, in either file_handling mode or placement -- it is
    ingested exactly where it is, however deeply nested (e.g.
    "Grateful Dead/1977/gd77-05-08"), and its stored folder_path is simply
    that path relative to LIBRARY_ROOT. Placement (under_artist_folder) only
    governs where a source arriving from OUTSIDE the root gets filed.

    Cancellation is handled HERE rather than by the caller because this is the
    only place that knows what has been written where — it means putting the
    files already moved back where they came from, which no caller could do.

    Returns:
        str — new folder path relative to library_root
    """
    src = Path(source_folder)
    try:
        in_root = src.resolve().is_relative_to(Path(library_root).resolve())
    except AttributeError:  # pragma: no cover - Python < 3.9 fallback
        try:
            src.resolve().relative_to(Path(library_root).resolve())
            in_root = True
        except ValueError:
            in_root = False
    if in_root:
        # R2-2: the on-disk relative path can be NFD bytes (an rsync from an
        # HFS+-era source, or a byte-exact Linux filesystem) even though
        # every other identity key for this folder (BulkIngestItem.rel_path,
        # the QualityAnalysis staging row) is NFC -- Recording.folder_path
        # must match those, not the raw bytes os.walk happened to see.
        rel = str(src.resolve().relative_to(Path(library_root).resolve())).replace(os.sep, "/")
        return unicodedata.normalize("NFC", rel)

    # The artist directory is a CONVENTION, not a law (2026-09-17). When it is
    # off, the show lands directly at the library root and dedupe happens
    # against every folder there instead of that one artist's shelf.
    dest_dir = (Path(library_root) / _sanitize_path(artist_name)
                if under_artist_folder else Path(library_root))
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Guard against silently merging into an already-existing folder of the
    # same canonical name — e.g. two undated-source "Various Artists" shows
    # at the same venue. Without this, mkdir(..., exist_ok=True) below would
    # happily reuse the OTHER recording's folder and both recordings' files
    # would land in one directory (2026-09-01, Ryman Auditorium 1964 bug
    # report). Collisions get the same "(2)", "(3)", ... suffix
    # rename_recording_folder() already applies post-ingest, via the shared
    # unique_folder_name() helper — see its docstring.
    folder_name = unique_folder_name(str(dest_dir), folder_name, keep_abs=str(src))
    dest_folder = dest_dir / folder_name
    dest_folder.mkdir(parents=True, exist_ok=True)

    audio_rename_map = audio_rename_map or {}
    files = [p for p in src.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files) or 1
    done  = 0
    if progress_cb:
        progress_cb(0, total)

    # (source, destination) for every file actually transferred, so a cancel can
    # be undone precisely.
    moved = []

    for p in files:
        # Poll BETWEEN files, never mid-file: a partially written file is the one
        # thing that would be genuinely hard to clean up.
        if cancel_cb is not None and cancel_cb():
            _undo_transfer(moved, dest_folder)
            raise IngestCancelled("ingest cancelled by user")

        rel  = str(p.relative_to(src)).replace(os.sep, "/")
        size = p.stat().st_size
        if p.suffix.lower() in AUDIO_EXTENSIONS:
            new_rel = resolve_ingest_file_path(rel, audio_rename_map, flatten)
            target  = dest_folder / new_rel
        else:
            # Preserve relative structure for everything else (Art/, loose .txt, ...).
            target = dest_folder / p.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(target))
        moved.append((p, target))
        done += size
        if progress_cb:
            progress_cb(done, total)

    # Files are gone from source; clear out the now-empty (or
    # empty-of-anything-useful) directory tree that's left behind.
    shutil.rmtree(str(src), ignore_errors=True)
    # The recording folder itself is gone — now check whether ITS parent
    # (typically the "Artist Name" staging folder in a Bulk Import
    # layout, e.g. Import/Artist Name/Show Folder/) is left empty too,
    # and remove it if so (Ryan, 2026-07-23 — applies to every ingest; see
    # _cleanup_empty_parent's own docstring for the safety guards).
    _cleanup_empty_parent(src)

    # Return path relative to library_root for storage in DB
    return str(dest_folder.relative_to(library_root))


# Folder-metadata cruft that shouldn't count as "real content" when deciding
# whether a staging folder is empty enough to remove — a folder Finder has
# ever opened almost always has a stray .DS_Store in it, which would
# otherwise block cleanup every single time.
_JUNK_FILENAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized"}

# macOS's SMB client renames a file to ".smbdelete<hex>" when a delete over a
# network share doesn't fully land, instead of just removing it (Ryan hit
# this directly, 2026-08-22 — Synology share, Finder refused with "locked"
# even though nothing was actually locked). The hex suffix is per-occurrence,
# so this can't be an exact-name match like the set above.
_JUNK_NAME_PREFIXES = (".smbdelete",)


def _is_junk_name(name):
    return name in _JUNK_FILENAMES or name.startswith(_JUNK_NAME_PREFIXES)

# Standard macOS/user directories that must never be auto-deleted even if
# they happen to be empty — this cleanup is meant for disposable Bulk Import
# staging folders (e.g. "Artist Name"), not general-purpose folders a
# user might legitimately empty out for unrelated reasons.
_PROTECTED_DIR_NAMES = {
    "Desktop", "Downloads", "Documents", "Music", "Movies",
    "Pictures", "Public", "Applications", "Library",

    # Flux's own top-level siblings under Flux Audio/ — IMPORT_DIR and
    # TRIAGE_DIRS in config.py, plus Training (Ryan's dev-only BAD-label
    # corpus folder — deliberately NOT wired into config.py per the
    # 2026-08-13 folder-structure decision, but still a real sibling on disk
    # that must never vanish; this name-only safety exclusion doesn't couple
    # the app to it functionally, so it doesn't reopen that decision).
    # Explicit ask (Ryan, 2026-08-23): these must never be removed even if
    # briefly empty between imports — unlike a "Artist Name" staging
    # folder, they are permanent structure, not disposable. NOTE: Trellis's
    # working folder is "Downloads" (2026-10-01, was "Download") and is
    # covered by the macOS "Downloads" entry above.
    "Backlog", "Training", "Workshop",
}


def _cleanup_empty_parent(folder):
    """
    After a MOVE ingest empties out and removes `folder` (the source show
    folder itself — already gone by the time this runs, see the rmtree
    above), remove ITS parent too if that parent is now empty. One level
    only — never walks further up the tree (Ryan's ask was specifically
    "the Artist Name source directory," singular, not an arbitrary climb
    toward the filesystem root).

    Best-effort and silent: this is a courtesy cleanup, not something that
    should ever fail — or even be noticed to fail — an otherwise-successful
    ingest. Junk it clears now includes ".smbdelete*" ghosts left behind by
    macOS's SMB client, alongside the pre-existing .DS_Store/etc — see
    _JUNK_NAME_PREFIXES. Each entry is removed independently, so one file the
    OS still won't release doesn't block clearing everything else, or block
    trying again on a later ingest.

    Refuses to touch anything that isn't unambiguously a disposable staging
    folder:
      - the user's home directory
      - a filesystem/volume root or mount point (e.g. "/Volumes/music")
      - a handful of standard macOS folders by name (Desktop, Downloads,
        Documents, ...) even if reached via a longer path, since deleting
        someone's Desktop because it happened to be empty would be a far
        worse outcome than leaving one harmless empty folder behind.
    """
    try:
        parent = Path(folder).parent.resolve()

        if parent == Path.home().resolve():
            return
        if parent == parent.parent:            # true filesystem root "/"
            return
        if os.path.ismount(str(parent)):        # volume root / mount point
            return
        if parent.name in _PROTECTED_DIR_NAMES:
            return
        if not parent.is_dir():
            return

        entries = list(parent.iterdir())
        real = [e for e in entries if not (e.is_file() and _is_junk_name(e.name))]
        if real:
            return   # still has real content — leave it alone

        # Best-effort PER FILE, not all-or-nothing: an .smbdelete ghost the OS
        # still considers busy (EBUSY, not "doesn't exist") shouldn't stop a
        # perfectly removable .DS_Store sitting right next to it from going —
        # attempt every entry, and only take the directory down once nothing
        # is left. Whether the busy one ever actually clears is outside what
        # any client-side code can force; next ingest through here tries again.
        all_removed = True
        for e in entries:
            try:
                e.unlink()
            except OSError:
                all_removed = False
        if all_removed:
            parent.rmdir()
    except OSError:
        pass   # best-effort — never let cleanup failure affect the ingest


class _NamingProxy:
    """Plain duck-typed stand-in for an ORM object, used only to feed
    app.utils.file_naming's engine (naming_context()/rename_plan()) from raw
    ingest-time dicts before any Recording/Track row exists yet."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def compute_audio_rename_map(tracks, scheme="number_title", template=None,
                              performance=None, source=None,
                              source_tag=None, etree_shnid=None):
    """
    Build a collision-safe mapping from each track's ORIGINAL rel_path (as
    scanned from the source folder — may carry a disc/set subdir prefix like
    "CD1/01.flac") to a new filename, via the shared naming engine
    (app.utils.file_naming.rename_plan — spec section 4). Renaming is safe
    for verification: FFP/MD5/ST5 are content hashes and don't change when a
    file is renamed — see [[project_checksum_format_preference]].

    scheme='original' (the identity template, {original}) is how a caller
    that wants no renaming asks for it — see app.api.ingest._do_confirm,
    which passes this when rename_files is off. There's no separate
    "identity map" code path: the 'original' preset renders each track's own
    current basename, which already IS the identity map.

    Args:
        tracks:      list of dicts with at least "track_number", "title",
                     and "filename" (the original rel_path/filename from
                     scan/tags), plus optionally "disc_number",
                     "disc_track_number", "set_number".
        scheme:      one of file_naming.PRESETS' keys, or 'custom'.
        template:    required when scheme == 'custom'.
        performance: the real Performance ORM object for this ingest (its
                     .artist/.venue relationships feed {artist}/{artist_abbr}/
                     {venue}/{location}/etc) — None renders those tokens
                     empty rather than raising.
        source, source_tag, etree_shnid: the recording-level values the
                     {source}/{source_tag}/{shnid} tokens read; a real
                     Recording row doesn't exist yet at this point in ingest.

    Returns:
        {original_rel_path_or_filename: new_filename}
    """
    if not tracks:
        return {}

    if scheme == "original":
        # No renaming happens under 'original' (spec section 4: {original}
        # literally IS the current stem) -- and 'original' never flattens
        # (flattens('original') is always False), so two tracks that share a
        # basename can only be two DIFFERENT directories' files once nesting
        # is preserved, never a real on-disk collision. Skip the engine
        # entirely rather than let its batch-wide _dedupe_names() invent a
        # "(2)" for a collision that can't happen here (caught by
        # test_do_confirm_keeps_names_and_nesting_in_keep_mode, a CD1/01.flac
        # + CD2/01.flac source -- the engine's dedup is about the FLATTENED
        # destination namespace, which 'original' never enters).
        return {t.get("filename"): os.path.basename(t.get("filename") or "")
                for t in tracks if t.get("filename")}

    track_likes = []
    for t in tracks:
        orig = t.get("filename") or ""
        if not orig:
            continue
        track_likes.append(_NamingProxy(
            track_number        = t.get("track_number"),
            title                = t.get("title") or "",
            file_path            = orig,
            original_file_path   = orig,
            disc_number          = t.get("disc_number"),
            disc_track_number    = t.get("disc_track_number"),
            set_number           = t.get("set_number"),
        ))
    if not track_likes:
        return {}

    perf = performance
    if perf is None:
        perf = _NamingProxy(start_year=None, start_month=None, start_day=None,
                            artist=None, venue=None, city=None, state=None,
                            country=None)

    rec_like = _NamingProxy(source=source, source_tag=source_tag,
                            etree_shnid=etree_shnid, performance=perf,
                            tracks=track_likes)

    plan = rename_plan(rec_like, scheme, template)
    rename_map = {}
    for track_like, _current, proposed in plan:
        # R1 (review, 2026-09-25): rename_plan() now returns a full proposed
        # REL PATH (B2's fix -- it carries the current directory prefix when
        # the scheme doesn't flatten, so its own dedupe never collides two
        # discs' same-named files). resolve_ingest_file_path() is what
        # decides the destination directory at ingest time (prepending the
        # ORIGINAL parent for a non-flattening scheme) -- storing the full
        # path here as well double-prepends it ("CD1/CD1/Dark Star.flac").
        # The map's job is only ever the FILENAME.
        rename_map[track_like.original_file_path] = os.path.basename(proposed)
    return rename_map


def _sanitize_path(name):
    """Strip characters illegal in macOS directory names."""
    return re.sub(r'[:/\x00]', '-', name).strip()
