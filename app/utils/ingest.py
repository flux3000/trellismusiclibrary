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
from difflib import get_close_matches
from pathlib import Path
from mutagen.flac import FLAC
from mutagen import MutagenError
from dateutil import parser as _dateutil_parser
from dateutil.parser import ParserError as _ParserError
import geonamescache as _geonamescache

from app.utils.format import format_partial_date
from app.utils.health import compute_health
from app.utils.folder_naming import unique_folder_name


# ── File classification ────────────────────────────────────────────────────────

AUDIO_EXTENSIONS    = {".flac", ".mp3", ".wav"}
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


def _parse_set_dir(name):
    """
    Parse a set/disc subdir name into (canonical_label, number).
    'disc one' -> ('Disc 1', 1);  'CD 02' -> ('CD 2', 2).  None if no match.

    Single source of truth for "is this folder a disc, and which one" —
    resolve_shows() and scan_folder() both ask it, so the triage queue and the
    ingest scanner can never disagree about whether a folder is one show.
    """
    name = (name or "").strip()
    m = _SET_DIGIT_RE.match(name)
    if m:
        prefix, number = m.group(1), int(m.group(2))
    else:
        m = _SET_WORD_RE.match(name)
        if not m:
            return None
        prefix, number = m.group(1), _SET_NUMBER_WORDS[m.group(2).lower()]
    label = _SET_PREFIX_LABELS.get(prefix.upper(), prefix.title())
    return f"{label} {number}", number

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


def _audio_subdirs(path):
    """Immediate subdirs of `path` that contain audio at any depth."""
    result = []
    try:
        for sub in os.scandir(path):
            if not sub.is_dir():
                continue
            for _, _, files in os.walk(sub.path):
                if any(os.path.splitext(f)[1].lower() in RESOLVE_AUDIO_EXTS
                       for f in files):
                    result.append(sub)
                    break
    except OSError:
        pass
    return result


def resolve_shows(path):
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
      - Has no audio at all → return as-is (the scanner grades it red).
    """
    if _root_audio_count(path) > 0:
        return [path]
    subs = _audio_subdirs(path)
    if not subs:
        return [path]
    if len(subs) == 1:
        return resolve_shows(subs[0].path)

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
    if sum(1 for s in subs if _parse_set_dir(s.name)) >= 2:
        return [path]

    result = []
    for sub in sorted(subs, key=lambda e: e.name.lower()):
        result.extend(resolve_shows(sub.path))
    return result


def resolve_shows_in_dir(source_dir):
    """
    Every show folder under one scanned directory.

    The top-level loop both `batch_scan` and the quality analyser run: each
    immediate subdirectory is resolved to its real show paths, handling
    arbitrary nesting (artist -> year -> show).
    """
    show_paths = []
    for entry in sorted(os.scandir(source_dir), key=lambda e: e.name.lower()):
        if entry.is_dir():
            show_paths.extend(resolve_shows(entry.path))
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


def _parse_filename_set(filename):
    """
    Parse a leading disc/track prefix off an audio filename.
    'D01T01. Show - Song.flac' -> ('Disc 1', 1, 1).  None if no match.

    Reuses _auto_set_label so a filename-carried disc produces exactly the
    same label vocabulary as a subdir-carried one ('CD 1', 'Disc 2', 'Set 1')
    — two sources of disc labels that disagree would surface as inconsistent
    set names across otherwise identical recordings.
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
    return label, disc, track


def _apply_filename_sets(result):
    """
    Second-chance set detection for flat folders whose FILENAMES carry the
    disc (see _FILENAME_SET_RE). Mutates `result` in place: stamps
    `set_number` on every audio file, re-sorts them into (disc, track) order
    and renumbers `index` continuously across discs.

    That continuous index is the whole point — it is the contract subdir
    detection provides, and it is what tells the rest of the pipeline
    (read_source_tags -> the ingest wizard -> compute_audio_rename_map) to
    stop trusting per-disc TRACKNUMBER tags.

    No-op unless every audio file matches and 2+ distinct discs are present.
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
    for i, (a, (label, _disc, _track)) in enumerate(order, start=1):
        a["index"]      = i
        a["set_number"] = label
        result["audio_files"].append(a)

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


def scan_folder(folder_path):
    """
    Walk a source folder and classify all files.

    Handles three structural cases:
      1. Flat folder — all audio in root (no subdirs with audio)
      2. Single transparent subdir — e.g. a 'flac/' subfolder; treated as flat
      3. Multi-set structure — subdirs named cd1/cd2, disc1/disc2, set1/set2, etc.
         Audio files get a 'set_number' field auto-populated from the subdir name.

    When multiple .txt files are present, the most likely info file is surfaced
    as text_files[0] based on filename scoring. All candidates are returned so
    the UI can offer a switcher.

    Returns:
      {
        "audio_files":       [ { index, filename, path, set_number } ],
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
    set_dirs = []   # [(abs_dirpath, label, number)]
    for e in subdirs:
        parsed = _parse_set_dir(e)
        if not parsed:
            continue
        label, num = parsed
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
            set_dirs.append((dpath, label, num))
    set_dirs.sort(key=lambda x: x[2])
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

    def _classify(fname, dirpath, set_label):
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
                # flattens audio into the library folder root regardless (see
                # compute_audio_rename_map / move_to_library) — rel_path here is
                # only used to locate the original file pre-flatten.
                "rel_path": os.path.relpath(full, folder_path),
                "path":     full,
                "set_number": set_label,   # None for flat; "CD 1" etc. for multi-set
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
        for fname in sorted(top_entries):
            full = os.path.join(folder_path, fname)
            if os.path.isfile(full):
                _classify(fname, folder_path, None)
        for dpath, label, _num in set_dirs:
            try:
                for fname in sorted(os.listdir(dpath)):
                    full = os.path.join(dpath, fname)
                    if os.path.isfile(full):
                        _classify(fname, dpath, label)
            except OSError:
                pass
        set_dir_paths = {dpath for dpath, _label, _num in set_dirs}
        for dirpath, dirnames, filenames in os.walk(folder_path):
            if dirpath == folder_path:
                dirnames[:] = [d for d in dirnames
                               if os.path.join(dirpath, d) not in set_dir_paths]
                continue   # root files already classified above
            for fname in sorted(filenames):
                _classify(fname, dirpath, None)
    elif scan_dirs is not None:
        # Structured walk: visit each (dir, set_label) pair, non-recursive
        seen_dirs = set()
        for dir_path, set_label in scan_dirs:
            if dir_path in seen_dirs:
                continue
            seen_dirs.add(dir_path)
            try:
                for fname in sorted(os.listdir(dir_path)):
                    full = os.path.join(dir_path, fname)
                    if os.path.isfile(full):
                        _classify(fname, dir_path, set_label)
            except OSError:
                pass
    else:
        # Flat walk
        for dirpath, _, filenames in os.walk(folder_path):
            for fname in sorted(filenames):
                _classify(fname, dirpath, None)

    # ── Filename-encoded sets ─────────────────────────────────────────────────
    # Flat folder, disc in the filename (d01t01…). Runs only when the subdir
    # pass found nothing, and after the walk so it can renumber the finished
    # audio list rather than restructure the traversal.
    if not sets_detected:
        _apply_filename_sets(result)

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

# Map FLAC tag keys → our field names
_TAG_MAP = {
    "ARTIST":          "artist",
    "ALBUM":           "album",
    "DATE":            "year",
    "CONCERTDATE":     "concert_date",
    "CONCERTVENUE":    "venue",
    "CONCERTLOCATION": "location",
    "RECORDINGSOURCE": "source",
    "LINEAGE":         "lineage",
    "TITLE":           "title",
    "TRACKNUMBER":     "track_number",
    "TRACKTOTAL":      "track_total",
}


def read_flac_tags(audio_files):
    """
    Read FLAC tags from all audio files.

    Returns:
      {
        "container": { artist, album, concert_date, venue, location, source, lineage },
        "tracks":    [ { index, filename, title, track_number, duration } ]
      }
    Container fields are read from the first successfully tagged file.
    """
    container = {}
    tracks    = []

    for f in audio_files:
        path = f["path"]
        try:
            audio = FLAC(path)
            tags  = audio.tags or {}

            # Capture container-level tags from first file that has them
            if not container:
                for tag_key, field in _TAG_MAP.items():
                    if tag_key in tags and field not in ("title", "track_number", "track_total"):
                        container[field] = tags[tag_key][0]

            # Full raw Vorbis comments (lowercased keys, single values unwrapped)
            # so the UI can show the same JSON as the recording view's File Tags.
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

        except (MutagenError, Exception):
            # Unreadable file — add placeholder so index stays consistent
            tracks.append({
                "index":        f["index"],
                "filename":     f["filename"],
                "rel_path":     f.get("rel_path", f["filename"]),
                "title":        None,
                "track_number": None,
                "duration":     None,
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
    included in container_tags.
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

    # ── Artist / album labels ─────────────────────────────────────────────────
    artist_name = perf.performer.name if (perf and perf.performer) else None
    album_parts = [p for p in [artist_name, concert_date, venue_name] if p]
    album_str   = " - ".join(album_parts) if album_parts else None

    container_tags = {}
    if artist_name:       container_tags["ARTIST"]          = artist_name
    if album_str:         container_tags["ALBUM"]           = album_str
    if perf and perf.start_year: container_tags["DATE"]     = str(perf.start_year)
    if concert_date:      container_tags["CONCERTDATE"]     = concert_date
    if venue_name:        container_tags["CONCERTVENUE"]    = venue_name
    if location_parts:    container_tags["CONCERTLOCATION"] = ", ".join(location_parts)
    if source_str:        container_tags["RECORDINGSOURCE"] = source_str
    if recording.lineage: container_tags["LINEAGE"]         = recording.lineage

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
            audio = FLAC(abs_path)
            entry["tags"] = {k: (v[0] if len(v) == 1 else v)
                             for k, v in (audio.tags or {}).items()}
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
    replaced entirely (clean write).

    Args:
        recording:    Recording ORM object with relationships loaded
        library_root: Absolute path string for the library root

    Returns:
        (n_written, errors) where errors is a list of (filename, message) tuples.
    """
    tracks = recording.tracks  # ordered by track_number via relationship
    container_tags, track_total = build_recording_tags(recording)

    n_written = 0
    errors    = []

    for track in tracks:
        abs_path = os.path.join(library_root, recording.folder_path, track.file_path)
        try:
            audio = FLAC(abs_path)

            # Clear all existing Vorbis comments
            audio.clear()

            # Container tags
            for tag_key, value in container_tags.items():
                audio[tag_key] = value

            # Track-specific tags
            audio["TITLE"]       = track.title
            audio["TRACKNUMBER"] = str(track.track_number)
            audio["TRACKTOTAL"]  = track_total
            if track.songwriter:
                audio["COMPOSER"] = track.songwriter

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

def _title_case(s):
    """Title-case a track title without mangling apostrophes (str.title() does 'Don'T')."""
    words = s.split()
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        if i == 0 or low not in _TC_LOWER:
            out.append(w[0].upper() + w[1:] if w else w)
        else:
            out.append(low)
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
    Try to extract a date from a line via dateutil.
    Only attempts lines with a strong date signal.
    Returns (year, month, day, raw_str) or None.
    """
    low       = line.lower()
    has_4yr   = bool(re.search(r"\b(19|20)\d{2}\b", line))
    has_2yr   = bool(re.search(r"\b\d{1,2}[-./]\d{1,2}[-./]\d{2}\b", line))
    has_month = any(m in low.split() for m in _MONTH_NAMES)

    if not (has_4yr or has_2yr or has_month):
        return None

    try:
        dt   = _dateutil_parser.parse(line, fuzzy=True, dayfirst=False)
        year = dt.year
        if year > _CURRENT_YEAR:       # 2-digit year fix: "89" → 1989
            year -= 100
        if not (1900 <= year <= _CURRENT_YEAR):
            return None
        return year, dt.month, dt.day, line.strip()
    except (_ParserError, ValueError, OverflowError):
        return None


def _parse_location(line):
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

        # Save date and location lines for keyword fallback, but skip them here
        is_date = _looks_like_date_line(line)
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

        return line.strip()

    # Pass 2 — keyword scan on skipped date/location lines
    for line in skipped_date_loc:
        low = line.lower()
        if any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in _VENUE_WORDS):
            segments = re.split(r",\s*|@\s*", line)
            for seg in segments:
                if any(re.search(r"\b" + re.escape(w) + r"\b", seg.lower()) for w in _VENUE_WORDS):
                    # Strip any leading date token (e.g. "1-28-89 Birchmere")
                    seg = re.sub(r"^\d{1,2}[-./]\d{1,2}[-./]\d{2,4}\s*", "", seg).strip()
                    if seg:
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
            title = _title_case(raw_title)
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
            result["artist"]       = line.title()
            result["artist_match"] = _fuzzy_match(line, known_artists or [])
            break

    # Date — first header line with a strong date signal
    for line in header_lines:
        parsed = _parse_date(line)
        if parsed:
            result["year"], result["month"], result["day"], result["date_str"] = parsed
            break

    # Venue — keyword scan then positional fallback
    venue_raw = _extract_venue(header_lines)
    if venue_raw:
        result["venue"]       = venue_raw.title()
        result["venue_match"] = _fuzzy_match(venue_raw, known_venues or [])

    # City / State / Country — first header line that validates
    for line in header_lines:
        city, state, country = _parse_location(line)
        if city or state or country:
            result["city"]    = city
            result["state"]   = state
            result["country"] = country
            break

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

    # Parse CONCERTLOCATION tag into city/state/country using the same
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
                        "raw":          t.get("raw", {}),
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

    resp["health"] = compute_health(resp)
    log_step(job, "done", f"health {resp['health']['score']} ({resp['health']['band']})")
    return resp


# ── File system operations ─────────────────────────────────────────────────────

def _undo_transfer(moved, dest_folder, behavior):
    """
    Put things back after a cancelled `move_to_library`.

    For a MOVE, every file already relocated goes back to where it came from —
    otherwise cancelling would silently scatter a show across two directories.
    For a COPY the source was never touched, so deleting the half-built
    destination is enough.

    Best-effort throughout: a failure to clean up must not mask the
    cancellation itself, which is what the user actually asked for.
    """
    if behavior == "move":
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


def move_to_library(source_folder, library_root, artist_name, folder_name,
                    behavior="copy", progress_cb=None, audio_rename_map=None,
                    cancel_cb=None):
    """
    Move or copy a source folder into the library under the artist directory.

    Audio files are always flattened into the destination folder's ROOT and
    renamed per `audio_rename_map` (original rel_path → new flat filename),
    regardless of how deeply nested they were in the source (CD1/, Disc 2/,
    flac/, ...). This keeps Track.file_path free of subdir prefixes and
    guarantees continuous, collision-free filenames even when a multi-disc
    source reset filenames independently per disc (the CD1/CD2 bug this
    replaced — 2026-07-14). Non-audio content (art, text files, etc.) keeps
    its original relative structure under dest_folder.

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
        behavior           : "copy" | "move"
        progress_cb         : callable(copied_bytes, total_bytes) | None — progress
        audio_rename_map   : {rel_path_or_basename: new_filename}, from
                              compute_audio_rename_map(). An audio file with
                              no entry keeps its original basename, still
                              flattened to dest_folder's root.
        cancel_cb          : callable() -> bool | None — polled BETWEEN files.
                              When it returns True this function undoes
                              everything it has done so far and raises
                              IngestCancelled.

    Cancellation is handled HERE rather than by the caller because this is the
    only place that knows what has been written where. For a copy that means
    deleting the half-built destination; for a MOVE it means putting the files
    already moved back where they came from, which no caller could do.

    Returns:
        str — new folder path relative to library_root
    """
    dest_dir = Path(library_root) / _sanitize_path(artist_name)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Guard against silently merging into an already-existing folder of the
    # same canonical name — e.g. two undated-source "Various Artists" shows
    # at the same venue. Without this, mkdir(..., exist_ok=True) below would
    # happily reuse the OTHER recording's folder and both recordings' files
    # would land in one directory (2026-09-01, Ryman Auditorium 1964 bug
    # report). Collisions get the same "(2)", "(3)", ... suffix
    # rename_recording_folder() already applies post-ingest, via the shared
    # unique_folder_name() helper — see its docstring.
    folder_name = unique_folder_name(str(dest_dir), folder_name)
    dest_folder = dest_dir / folder_name
    dest_folder.mkdir(parents=True, exist_ok=True)

    audio_rename_map = audio_rename_map or {}
    src   = Path(source_folder)
    files = [p for p in src.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files) or 1
    done  = 0
    if progress_cb:
        progress_cb(0, total)

    # (source, destination) for every file actually transferred, so a cancel can
    # be undone precisely. Only meaningful for behavior="move"; a copy is undone
    # by deleting the destination tree.
    moved = []

    for p in files:
        # Poll BETWEEN files, never mid-file: a partially written file is the one
        # thing that would be genuinely hard to clean up.
        if cancel_cb is not None and cancel_cb():
            _undo_transfer(moved, dest_folder, behavior)
            raise IngestCancelled("ingest cancelled by user")

        rel  = str(p.relative_to(src)).replace(os.sep, "/")
        size = p.stat().st_size
        if p.suffix.lower() in AUDIO_EXTENSIONS:
            # Flatten: destination has no subdir, regardless of source nesting.
            new_name = audio_rename_map.get(rel) or audio_rename_map.get(p.name) or p.name
            target   = dest_folder / new_name
        else:
            # Preserve relative structure for everything else (Art/, loose .txt, ...).
            target = dest_folder / p.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        if behavior == "move":
            shutil.move(str(p), str(target))
            moved.append((p, target))
        else:
            shutil.copy2(str(p), str(target))
        done += size
        if progress_cb:
            progress_cb(done, total)

    if behavior == "move":
        # Files are gone from source; clear out the now-empty (or
        # empty-of-anything-useful) directory tree that's left behind.
        shutil.rmtree(str(src), ignore_errors=True)
        # The recording folder itself is gone — now check whether ITS parent
        # (typically the "Performer Name" staging folder in a Bulk Import
        # layout, e.g. Import/Performer Name/Show Folder/) is left empty too,
        # and remove it if so (Ryan, 2026-07-23 — applies to every
        # move-behavior ingest, not just Bulk Import; see
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
# staging folders (e.g. "Performer Name"), not general-purpose folders a
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
    # briefly empty between imports — unlike a "Performer Name" staging
    # folder, they are permanent structure, not disposable. NOTE: "Download"
    # (singular) is Flux's own folder and distinct from macOS's "Downloads"
    # above; both are listed, neither substitutes for the other.
    "Download", "Backlog", "Training", "Workshop",
}


def _cleanup_empty_parent(folder):
    """
    After a MOVE ingest empties out and removes `folder` (the source show
    folder itself — already gone by the time this runs, see the rmtree
    above), remove ITS parent too if that parent is now empty. One level
    only — never walks further up the tree (Ryan's ask was specifically
    "the Performer Name source directory," singular, not an arbitrary climb
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


def compute_audio_rename_map(tracks):
    """
    Build a collision-safe mapping from each track's ORIGINAL rel_path (as
    scanned from the source folder — may carry a disc/set subdir prefix like
    "CD1/01.flac") to a new flat filename to use once the recording is moved
    into the library.

    Library audio is always flattened to the folder root and renamed on
    ingest (Ryan's "always flatten + rename" decision, 2026-07-14) — this is
    what fixes multi-disc sources whose per-disc TRACKNUMBER tags reset and
    collide (e.g. two files both literally named "01.flac"). Renaming is
    safe for verification: FFP/MD5/ST5 are content hashes and don't change
    when a file is renamed — see [[project_checksum_format_preference]].

    Naming pattern: "NN - Title.ext", zero-padded to the width of the
    highest track_number (min 2 digits) so names sort correctly once a
    recording has 10+ tracks.

    Args:
        tracks: list of dicts with at least "track_number", "title", and
                "filename" (the original rel_path/filename from scan/tags).

    Returns:
        {original_rel_path_or_filename: new_flat_filename}
    """
    if not tracks:
        return {}

    max_num = max((t.get("track_number") or 0) for t in tracks) or len(tracks)
    width   = max(2, len(str(max_num)))

    rename_map = {}
    used_names = set()
    for t in tracks:
        orig = t.get("filename") or ""
        if not orig:
            continue
        ext   = os.path.splitext(orig)[1].lower()
        num   = t.get("track_number") or 0
        title = _sanitize_filename(t.get("title") or f"Track {num}")
        base  = f"{str(num).zfill(width)} - {title}{ext}"
        name  = base
        n = 2
        while name.lower() in used_names:
            name = f"{str(num).zfill(width)} - {title} ({n}){ext}"
            n += 1
        used_names.add(name.lower())
        rename_map[orig] = name
    return rename_map


def _sanitize_filename(name):
    """Strip characters illegal/awkward in filenames (macOS + Windows-safe,
    since library folders sometimes get shared to non-Mac drives) and
    collapse whitespace. Distinct from _sanitize_path(), which is only used
    for directory names and deliberately leaves "/" untouched."""
    name = re.sub(r'[\\/:*?"<>|\x00]', '-', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name or "Track"


def _sanitize_path(name):
    """Strip characters illegal in macOS directory names."""
    return re.sub(r'[:/\x00]', '-', name).strip()
