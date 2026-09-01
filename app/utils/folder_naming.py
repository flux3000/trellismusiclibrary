"""
utils/folder_naming.py — Canonical folder name generation.

Convention:
  {Artist} - {date} - {Venue} - {Location} ({Source})

Date formats (partial dates degrade gracefully):
  Full:       1977-05-08
  Month only: 1977-06
  Year only:  1977
  Multi-day:  1977-05-08 to 1977-05-09

Source formats:
  source only: SBD

Examples:
  Grateful Dead - 1977-05-08 - Barton Hall - Ithaca, NY (SBD)
  Bela Fleck - 1998-01-15 - Bill and Claire's Living Room - Hickory, NC (SBD)
  Unknown Artist - 1963 - Unknown Venue - Unknown Location
"""

import os
import re


def _format_date(year, month, day):
    """Build a date string from nullable year/month/day integers."""
    if not year:
        return "Unknown Date"
    if month and day:
        return f"{year}-{month:02d}-{day:02d}"
    if month:
        return f"{year}-{month:02d}"
    return str(year)


def _format_date_range(start_year, start_month, start_day,
                       end_year, end_month, end_day):
    """
    Build a date or date-range string.
    If end date is set and differs from start, render as 'START to END'.
    """
    start = _format_date(start_year, start_month, start_day)
    if not end_year:
        return start
    end = _format_date(end_year, end_month, end_day)
    if end == start:
        return start
    return f"{start} to {end}"


def _format_source(source):
    """
    "Other" is a catch-all bucket, not a meaningful source label — it loses the
    context that it's the source field — so it is dropped from the name.
    """
    if not source or source == "Other":
        return None
    return source


def _format_location(city, state, country):
    """Build a location string from available fields."""
    if city and state:
        return f"{city}, {state}"
    if city and country:
        return f"{city}, {country}"
    if city:
        return city
    if state:
        return state
    if country:
        return country
    return "Unknown Location"


def _sanitize(name):
    """Remove characters illegal in macOS filenames."""
    # macOS only disallows : and / in filenames (and NUL)
    return re.sub(r'[:/\x00]', '-', name).strip()


def build_folder_name(
    artist_name,
    start_year=None, start_month=None, start_day=None,
    end_year=None,   end_month=None,   end_day=None,
    venue_name=None,
    city=None, state=None, country=None,
    source=None,
):
    """
    Generate the canonical folder name for a recording.

    Args:
        artist_name     : str  — performer display name
        start/end date  : nullable ints
        venue_name      : str | None
        city/state/country : str | None
        source          : str | None  — e.g. "SBD", "AUD"

    Returns:
        str — folder name safe for macOS filesystem
    """
    artist  = _sanitize(artist_name or "Unknown Artist")
    date    = _format_date_range(start_year, start_month, start_day,
                                 end_year,   end_month,   end_day)
    venue   = _sanitize(venue_name or "Unknown Venue")
    loc     = _sanitize(_format_location(city, state, country))
    src     = _format_source(source)

    # Base: Artist - Date - Venue - Location
    name = f"{artist} - {date} - {venue} - {loc}"

    # Append source in parens if known
    if src:
        name = f"{name} ({_sanitize(src)})"

    return name


def build_folder_name_from_recording(recording, performance, performer, venue):
    """
    Convenience wrapper — builds folder name directly from ORM objects.

    Args:
        recording   : Recording model instance
        performance : Performance model instance
        performer   : Performer model instance
        venue       : Venue model instance | None
    """
    # Resolve location — performance overrides event, venue is canonical
    if venue:
        city    = venue.city
        state   = venue.state
        country = venue.country
    else:
        city    = performance.city
        state   = performance.state
        country = performance.country

    return build_folder_name(
        artist_name     = performer.name,
        start_year      = performance.start_year,
        start_month     = performance.start_month,
        start_day       = performance.start_day,
        end_year        = performance.end_year,
        end_month       = performance.end_month,
        end_day         = performance.end_day,
        venue_name      = venue.name if venue else None,
        city            = city,
        state           = state,
        country         = country,
        source          = recording.source,
    )


def _dedupe_name(canonical_name, n):
    """
    Append a disambiguating digit — inside the source parens if there is one
    ("... (SBD)" -> "... (SBD 2)"), otherwise a bare "... (2)". Matches
    Ryan's existing manual practice for colliding folder names.
    """
    m = re.match(r'^(.*)\(([^)]*)\)\s*$', canonical_name)
    if m:
        base, inner = m.group(1).rstrip(), m.group(2)
        return f"{base} ({inner} {n})"
    return f"{canonical_name} ({n})"


def unique_folder_name(parent_abs, canonical_name, keep_abs=None):
    """
    Find a folder name under parent_abs (an absolute directory path) that
    doesn't collide with anything already on disk there — starting from
    canonical_name and walking _dedupe_name()'s "(2)", "(3)", ... suffixes
    on each collision.

    Shared by every path that plants or renames a recording folder to its
    canonical name — ingest (move_to_library, app/utils/ingest.py) and
    metadata-driven rename (rename_recording_folder, below) both go through
    this, so two recordings with identical Artist/Date/Venue/Location/Source
    (e.g. two undated-source "Various Artists" shows at the same venue) get
    the same disambiguating suffix regardless of which path creates or
    renames the collision. Before 2026-09-01 only the rename path had this
    check — ingest had none, and would silently merge the second recording's
    files into the first's folder (Ryman Auditorium 1964 bug report).

    keep_abs: pass the CURRENT absolute path of the folder being renamed so
    a no-op rename (the new name computes to the folder's own existing name)
    doesn't count as a collision against itself. Omit for a brand-new folder
    (ingest), where nothing should be excluded from the check.
    """
    name       = canonical_name
    target_abs = os.path.join(parent_abs, name)
    n = 2
    while (os.path.exists(target_abs)
           and (keep_abs is None
                or os.path.normpath(target_abs) != os.path.normpath(keep_abs))):
        name       = _dedupe_name(canonical_name, n)
        target_abs = os.path.join(parent_abs, name)
        n += 1
    return name


def unique_file_name(parent_abs, filename, keep_abs=None):
    """
    unique_folder_name()'s counterpart for a FILE: the disambiguating digit
    goes before the extension ("01 - Song.flac" -> "01 - Song (2).flac"),
    which is the convention compute_audio_rename_map() already uses for two
    tracks that collide within a single ingest batch.

    This exists because os.rename() on POSIX REPLACES an existing destination
    FILE silently — unlike a directory, which at least errors when non-empty.
    So a track retitled into another track's filename does not collide, it
    DELETES that track's audio. Two tracks can reach the same name honestly:
    same track number and same title, or any folder that ended up holding two
    shows (both numbered from 01).

    keep_abs: the file's own current absolute path, so a rename that resolves
    back to where it already is isn't treated as a collision with itself.
    """
    stem, ext  = os.path.splitext(filename)
    name       = filename
    target_abs = os.path.join(parent_abs, name)
    n = 2
    while (os.path.exists(target_abs)
           and (keep_abs is None
                or os.path.normpath(target_abs) != os.path.normpath(keep_abs))):
        name       = f"{stem} ({n}){ext}"
        target_abs = os.path.join(parent_abs, name)
        n += 1
    return name


def _other_recordings_in_folder(recording, folder_rel):
    """
    How many OTHER recordings claim this same folder_path.

    Normally zero — but the ingest merge bug (fixed 2026-09-01) left real
    libraries with folders shared by several recordings; Ryan's had eight
    Opry transcription shows, 146 files, in one directory. Renaming a shared
    folder to match ONE of them drags the other seven's audio along and
    leaves their folder_path pointing at a directory that no longer exists.

    Returns 0 when the question cannot be asked at all (no app/DB context —
    a script, or a plain stand-in object in a unit test). That is the
    permissive direction on purpose: outside a real session there is no
    shared DB state to protect, and a naming helper must not hard-depend on
    a database being present.
    """
    try:
        from app.extensions import db
        from app.models.recording import Recording
        return (db.session.query(Recording)
                .filter(Recording.folder_path == folder_rel,
                        Recording.id != getattr(recording, "id", None))
                .count())
    except Exception:  # noqa: BLE001 — see docstring
        return 0


def rename_recording_folder(recording, library_root):
    """
    Rename a recording's on-disk folder to match its CURRENT metadata, if it
    has drifted from what's on disk (e.g. a date or venue correction after
    ingest, when the folder was only ever named once, at ingest time).

    Decided 2026-07-25 (Context Library — Ingest section): the folder name is
    Flux's own construction from metadata, never the taper's artefact, so it
    takes no preference and no button — it just follows the metadata whenever
    a Performance or Recording field that feeds build_folder_name() changes.
    Built 2026-08-09.

    NON-FATAL BY DESIGN: a filesystem problem must never block a metadata
    save. On any failure this leaves `recording.folder_path` exactly as it
    was and returns an error string for the caller to surface; on success it
    updates `recording.folder_path` (NOT committed — the caller owns the
    transaction, same convention as every other mutation helper in this app)
    and returns None. Collisions get a digit inside the parens, or a bare
    "(2)" with no source segment — never a renumber of an existing suffix,
    since this always computes fresh from the canonical name outward.

    Only renames the FOLDER itself. Renaming the audio files inside it is the
    separate, still-unbuilt "Update File Names" button (Context Library) —
    deliberately kept apart because file renaming has to interact with
    checksum matching (.ffp/.md5 files reference file NAMES) in a way a
    folder rename never does.
    """
    performance = recording.performance
    if not performance:
        return None
    performer = performance.performer
    if not performer:
        return None
    venue = performance.venue

    old_rel = (recording.folder_path or "").rstrip("/")
    if not old_rel:
        return None
    old_name = os.path.basename(old_rel)
    new_name = build_folder_name_from_recording(recording, performance, performer, venue)
    if new_name == old_name:
        return None  # already correct — the common case on every save

    parent_rel = os.path.dirname(old_rel)
    old_abs    = os.path.join(str(library_root), old_rel)
    if not os.path.isdir(old_abs):
        return f"Folder not found on disk, could not rename: {old_rel}"

    # A folder several recordings share is not this recording's to rename.
    # Refuse rather than repair: renaming it would move the others' audio to
    # a name that describes only THIS recording's metadata, and silently
    # invalidate their folder_path. Non-fatal like every other failure here,
    # so the metadata save itself still goes through. 2026-09-01.
    shared = _other_recordings_in_folder(recording, old_rel)
    if shared:
        return (f"Folder not renamed — {shared} other recording"
                f"{'' if shared == 1 else 's'} share this folder, and renaming "
                f"it would move their files too: {old_rel}")

    parent_abs  = os.path.join(str(library_root), parent_rel)
    target_name = unique_folder_name(parent_abs, new_name, keep_abs=old_abs)
    target_abs  = os.path.join(parent_abs, target_name)

    try:
        os.rename(old_abs, target_abs)
    except OSError as e:
        return str(e)

    recording.folder_path = os.path.join(parent_rel, target_name) if parent_rel else target_name
    return None
