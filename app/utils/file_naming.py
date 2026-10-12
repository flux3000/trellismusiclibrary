"""
app/utils/file_naming.py — the naming engine (spec section 4).

Pure functions, no app context: every input is passed in explicitly so the
module can be unit-tested and used from a script without a Flask app or a
database session.

Grammar: curly-brace tokens with optional colon-chained modifiers
({title:lower:underscore}), optional square-bracket groups that render only
when every token inside them resolves to a value, and literal text
everywhere else. The file's own extension is never part of a template — it
is appended by the caller of render()/rename_plan(). Names are made safe for
Windows as well as macOS by windows_safe(); see its docstring for the rules.

rename_plan() is the one renderer used by ingest, retitle, Rename Files and
the preview endpoint — see app/api/naming.py, app/api/tracks.py and
app/api/ingest.py.
"""

import os
import re


class TemplateError(ValueError):
    """Raised by parse_template() for a template that cannot be used."""


# ── Grammar ──────────────────────────────────────────────────────────────────

TOKENS = {
    "artist", "artist_abbr", "date", "year", "month", "day",
    "venue", "city", "state", "country", "location",
    "source", "source_tag", "shnid",
    "disc", "track_in_disc", "set", "set_label", "track_in_set",
    "track", "title", "original",
}

MODIFIERS = {"lower", "upper", "nospace", "underscore"}

# ── Windows-safe names ───────────────────────────────────
# One rule set for every file and folder name Trellis writes, so a library
# copied to Windows, an exFAT drive or a peer never meets a name it cannot
# create. Folder names (folder_naming._sanitize, ingest._sanitize_path) use
# this too.

_SEGUE_RE    = re.compile(r"\s*[<>|:]+\s*")       # Tweezer>Light, Encore: X
_EDGE_SEGUE  = re.compile(r"^\s*[<>|:]+\s*|\s*[<>|:]+\s*$")
_CONTROL_RE  = re.compile(r"[\x00-\x1f]")
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}

# Longest relative path (folder_path + "/" + file) Trellis will write. Leaves
# ~60 characters of Windows' 260 for the library root (C:\Users\...\Library\).
MAX_REL_PATH = 200


def windows_safe(text, whole_name=True):
    """
    Make one name component legal on Windows and macOS. Only illegal
    characters are touched, so an already-legal name comes back unchanged
    (no whitespace collapsing, no case changes) and never triggers a rename.

      >  <  |  :   become " - "   (Tweezer>Light -> Tweezer - Light;
                                   Encore: X -> Encore - X); at either end
                                   of the name they are dropped
      "           becomes '
      ?  *        are removed      (M*A*S*H* -> MASH)
      /  \\        become "-"
      control characters removed; a reserved device name (CON, NUL,
      COM1...) gets "_" appended.

    whole_name: strip trailing spaces and dots, which Windows forbids at the
    end of a whole name (a folder). A file STEM passes False: "E.M.D..flac"
    is legal, its name ends in "flac".
    """
    t = _CONTROL_RE.sub("", text or "")
    t = _EDGE_SEGUE.sub("", t)
    t = _SEGUE_RE.sub(" - ", t)
    t = t.replace('"', "'")
    t = t.replace("?", "").replace("*", "")
    t = t.replace("/", "-").replace("\\", "-")
    if whole_name:
        t = t.rstrip(" .")
    if t.split(".")[0].strip().upper() in _WIN_RESERVED:
        t += "_"
    return t


def sanitize_filename_part(text):
    """A file stem made Windows-safe. Kept under its old name for render()."""
    return windows_safe(text, whole_name=False)


def _words(text):
    return set(re.findall(r"[^\W\d_]+", (text or "").lower()))


def _drop_repeated_artist(stem, folder_rel):
    """
    Remove the one inner " - " segment of a file stem that restates the
    artist from the recording's folder ("01 - 01 Stephen Layton; Britten
    Sinfonia, Polyphony - Mid-Winter Songs - 4" -> "01 - Mid-Winter Songs - 4").
    A segment counts when at least half its words, and at least two (one for
    a one-word artist), appear in the artist folder's name. The first and
    last segments (track number, title) are never dropped.
    """
    artist = _words((folder_rel or "").split("/")[0])
    segs = stem.split(" - ")
    if not artist or len(segs) < 3:
        return stem
    need = min(2, len(artist))
    for i in range(1, len(segs) - 1):
        w = _words(segs[i])
        hit = len(w & artist)
        if w and hit >= need and hit / len(w) >= 0.5:
            return " - ".join(segs[:i] + segs[i + 1:])
    return stem


def fit_name(rel_name, folder_rel, max_len=MAX_REL_PATH):
    """
    Shorten the file STEM of rel_name (which may carry a subdir prefix) so
    folder_rel + "/" + rel_name fits in max_len, cutting at a word boundary.
    The extension and any subdir prefix are kept. Titles in the database are
    never shortened; only the name on disk is.
    """
    head, base = os.path.split(rel_name)
    stem, ext = os.path.splitext(base)
    used = (len(folder_rel) + 1 if folder_rel else 0) + (len(head) + 1 if head else 0)
    budget = max(max_len - used - len(ext) - 4, 12)   # 4 spare for a " (2)" dedupe suffix
    if len(stem) <= budget:
        return rel_name
    # Before cutting any title, drop an artist name the file repeats from the
    # folder: it is the least informative part of the name.
    stem = _drop_repeated_artist(stem, folder_rel)
    if len(stem) <= budget:
        return f"{head}/{stem}{ext}" if head else f"{stem}{ext}"
    cut = stem[:budget]
    space = cut.rfind(" ")
    if space >= budget * 0.6:
        cut = cut[:space]
    cut = cut.rstrip(" -.,;(")
    return f"{head}/{cut}{ext}" if head else f"{cut}{ext}"


def safe_name_plan(folder_rel, file_paths):
    """
    Windows-safe, length-fitted replacements for a recording's EXISTING file
    names: same naming, only illegal characters and over-long names fixed.
    Unlike rename_plan() it never applies a naming scheme, so a library's
    own naming survives. Pure, no disk or DB access.

    Returns new rel paths aligned with file_paths (equal where no change).
    """
    out = []
    for cur in file_paths:
        cur = cur or ""
        parts = cur.split("/")
        *dirs, base = parts
        stem, ext = os.path.splitext(base)
        safe_dirs = [windows_safe(d) or d for d in dirs]
        safe_base = (windows_safe(stem, whole_name=False).strip() or stem) + ext
        new = "/".join(safe_dirs + [safe_base])
        out.append(fit_name(new, folder_rel or ""))
    # Two names can land on the same safe name ("A?" and "A"); keep them apart.
    return _dedupe_names(out)


def parse_template(text):
    """
    Parse a template string into a tree of ('lit', str) / ('token', name,
    [modifiers]) / ('group', [parts]) tuples.

    Raises TemplateError for: an unknown token, an unbalanced bracket
    ('{'/'}' or '['/']'), or a path separator ('/' or '\\') anywhere in the
    template (a template must never create a subdirectory).
    """
    if text is None:
        raise TemplateError("template is empty")
    if "/" in text or "\\" in text:
        raise TemplateError("template may not contain a path separator")

    pos = 0
    length = len(text)

    def parse_seq(depth):
        nonlocal pos
        parts = []
        buf = ""
        while pos < length:
            c = text[pos]
            if c == "{":
                end = text.find("}", pos)
                if end == -1:
                    raise TemplateError("unbalanced brace")
                if buf:
                    parts.append(("lit", buf)); buf = ""
                inner = text[pos + 1:end]
                name, *mods = inner.split(":")
                if name not in TOKENS:
                    raise TemplateError(f"unknown token: {{{name}}}")
                for m in mods:
                    if m not in MODIFIERS:
                        raise TemplateError(f"unknown modifier: :{m}")
                parts.append(("token", name, mods))
                pos = end + 1
            elif c == "[":
                if buf:
                    parts.append(("lit", buf)); buf = ""
                pos += 1
                parts.append(("group", parse_seq(depth + 1)))
            elif c == "]":
                if depth == 0:
                    raise TemplateError("unbalanced bracket")
                if buf:
                    parts.append(("lit", buf)); buf = ""
                pos += 1
                return parts
            else:
                buf += c
                pos += 1
        if depth != 0:
            raise TemplateError("unbalanced bracket")
        if buf:
            parts.append(("lit", buf))
        return parts

    return parse_seq(0)


# ── Presets (spec section 4) ─────────────────────────────────────────────────

PRESETS = {
    "original":     "{original}",
    "number_title": "{track} - {title}",
    # "etree" in the picker: one continuous track number, no disc. The older
    # "etree" key below keeps its with-disc meaning so a saved choice does not
    # change under anyone; the picker calls it "etree with disc".
    "etree_tracks": "{artist_abbr}{date}[.{source:lower}][.{source_tag}][.{shnid}].t{track}",
    "etree":        "{artist_abbr}{date}[.{source:lower}][.{source_tag}][.{shnid}].[d{disc}]t{track_in_disc}",
    "etree_sets":   "{artist_abbr}{date}[.{source:lower}][.{source_tag}][.{shnid}][s{set}]t{track_in_set}",
}


def flattens(scheme, template=None):
    """
    Whether ingest should flatten a multi-disc/-set source into the
    recording folder's root under this scheme.

    False for 'original' (nesting preserved); true for the three built-in
    position-carrying presets. For 'custom', true only when the template
    contains a continuous {track}, or pairs a disc/set token with its own
    per-disc/-set position token — the same signal a position-carrying
    preset gives, generalised (spec section 4).
    """
    if scheme == "original":
        return False
    if scheme in ("number_title", "etree_tracks", "etree", "etree_sets"):
        return True
    if scheme == "custom":
        t = template or ""
        if "{track}" in t:
            return True
        if "{disc}" in t and "{track_in_disc}" in t:
            return True
        if "{set}" in t and "{track_in_set}" in t:
            return True
        return False
    return False


# ── Context ──────────────────────────────────────────────────────────────────

def _initials(name):
    """Lowercase initials of a name's words ('Grateful Dead' -> 'gd')."""
    words = [w for w in re.split(r"\s+", (name or "").strip()) if w]
    return "".join(w[0] for w in words).lower() or None


def _location(city, state, country):
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
    return None


def _set_number_from_label(label):
    """'Set 2' -> 2; 'Encore' -> None (no digit in the label)."""
    if not label:
        return None
    m = re.search(r"(\d+)", label)
    return int(m.group(1)) if m else None


def _derive_track_in_set(tracks, track):
    """
    1-based position of `track` within its own set, grouping tracks by
    set_number in the order each set's FIRST track appears (by
    track_number). None when `track` has no set_number. Mirrors the
    set_track_number derivation in app/utils/serialize.py — see spec
    section 5: sets are recomputed on read, never stored.
    """
    if not track.set_number:
        return None
    # Position within the SAME label, ordered by track_number, among the
    # tracks sharing that label.
    same = sorted(
        (t for t in tracks if t.set_number == track.set_number),
        key=lambda t: t.track_number or 0,
    )
    for i, t in enumerate(same, start=1):
        if t is track:
            return i
    return None


def naming_context(recording, performance, artist, venue, track, tracks):
    """
    Build the token-value dict render() consumes for ONE track.

    tracks: the recording's full track list (used to compute {track}'s
    zero-pad width from the highest track_number, and to derive
    {track_in_set}). Pass the same list for every track of one recording.
    """
    tracks = list(tracks or [])
    numbers = [t.track_number for t in tracks if t.track_number is not None]
    track_width = max(2, len(str(max(numbers)))) if numbers else 2

    year  = str(performance.start_year)          if performance and performance.start_year  else None
    month = f"{performance.start_month:02d}"     if performance and performance.start_month else None
    day   = f"{performance.start_day:02d}"       if performance and performance.start_day   else None
    date_str = None
    if year:
        if month and day:
            date_str = f"{year}-{month}-{day}"
        elif month:
            date_str = f"{year}-{month}"
        else:
            date_str = year

    venue_name = venue.name if venue else None
    city    = (venue.city    if venue else None) or (performance.city    if performance else None)
    state   = (venue.state   if venue else None) or (performance.state   if performance else None)
    country = (venue.country if venue else None) or (performance.country if performance else None)

    artist_name = artist.name if artist else None
    artist_abbr = (getattr(artist, "abbreviation", None) if artist else None) or _initials(artist_name)

    set_label = track.set_number if track else None
    original = None
    if track:
        orig = getattr(track, "original_file_path", None) or track.file_path
        if orig:
            original = os.path.splitext(os.path.basename(orig))[0]

    return {
        "artist":         artist_name,
        "artist_abbr":    artist_abbr,
        "date":           date_str,
        "year":           year,
        "month":          month,
        "day":            day,
        "venue":          venue_name,
        "city":           city,
        "state":          state,
        "country":        country,
        "location":       _location(city, state, country),
        "source":         recording.source     if recording else None,
        "source_tag":     recording.source_tag if recording else None,
        "shnid":          (str(recording.etree_shnid)
                          if (recording and recording.etree_shnid is not None) else None),
        "disc":           track.disc_number       if track else None,
        "track_in_disc":  track.disc_track_number if track else None,
        "set":            _set_number_from_label(set_label),
        "set_label":      set_label,
        "track_in_set":   _derive_track_in_set(tracks, track) if track else None,
        "track":          track.track_number if track else None,
        "title":          (track.title or "") if track else "",
        "original":       original,
        "_track_width":   track_width,
    }


# ── Rendering ────────────────────────────────────────────────────────────────

def _apply_modifiers(value, mods):
    for m in mods:
        if m == "lower":
            value = value.lower()
        elif m == "upper":
            value = value.upper()
        elif m == "nospace":
            value = value.replace(" ", "")
        elif m == "underscore":
            value = value.replace(" ", "_")
    return value


def _render_token(name, mods, ctx):
    """Return the token's rendered text, or None when it has no value."""
    width = ctx["_track_width"]

    if name == "track":
        v = ctx["track"]
        val = None if v is None else str(v).zfill(width)
    elif name in ("track_in_disc", "track_in_set"):
        v = ctx[name]
        if v is None:
            # Falls back to the continuous track number's own rendering.
            tv = ctx["track"]
            val = None if tv is None else str(tv).zfill(width)
        else:
            val = str(v).zfill(2)
    elif name == "title":
        t = (ctx["title"] or "").strip()
        if t:
            val = ctx["title"]
        else:
            tv = ctx["track"]
            num = str(tv).zfill(width) if tv is not None else "??"
            val = f"Track {num}"
    elif name in ("disc", "set"):
        v = ctx[name]
        val = None if v is None else str(v)
    else:
        v = ctx.get(name)
        val = None if v is None else str(v)

    if val is None:
        return None
    return _apply_modifiers(val, mods)


def _render_parts(parts, ctx):
    """Returns (text, unresolved) — unresolved is True iff at least one
    token directly in `parts` (not inside a nested group) resolved to
    None, which tells an enclosing group to drop entirely."""
    out = []
    unresolved = False
    for part in parts:
        kind = part[0]
        if kind == "lit":
            out.append(part[1])
        elif kind == "token":
            val = _render_token(part[1], part[2], ctx)
            if val is None:
                unresolved = True
            else:
                out.append(val)
        elif kind == "group":
            sub_text, sub_unresolved = _render_parts(part[1], ctx)
            if not sub_unresolved:
                out.append(sub_text)
    return "".join(out), unresolved


def render(template, ctx):
    """
    Render a template (a string, or an already-parsed tree from
    parse_template()) against a context from naming_context().

    Raises TemplateError when the rendered output is empty (a filename with
    nothing in it is not usable).
    """
    parts = parse_template(template) if isinstance(template, str) else template
    text, _ = _render_parts(parts, ctx)
    text = sanitize_filename_part(text).strip()
    if not text:
        raise TemplateError("template produced an empty name")
    return text


def _dedupe_names(names):
    """
    In-memory collision suffixing for a batch of proposed names ('01.flac',
    '01.flac' -> '01.flac', '01 (2).flac'), case-insensitive. No disk
    access — rename_plan()/the preview are pure. The actual on-disk
    collision guard for an existing file outside this batch is
    unique_file_name() (app/utils/folder_naming.py), applied by whichever
    caller actually writes files.
    """
    seen = {}
    out = []
    for name in names:
        stem, ext = os.path.splitext(name)
        key = name.lower()
        n = seen.get(key, 0) + 1
        seen[key] = n
        out.append(name if n == 1 else f"{stem} ({n}){ext}")
    return out


def rename_plan(recording, scheme, template=None):
    """
    [(track, current_rel_path, proposed_rel_path), ...] for every track of
    `recording`, ordered by track_number. Pure: no disk access.

    scheme: one of the PRESETS keys, or 'custom' (template required then).

    proposed_rel_path carries the same directory prefix as current_rel_path
    when this scheme does not flatten (flattens() == False, e.g. 'original')
    -- the same rule the Rename Files endpoint applies when it turns a plan
    into actual moves. Deduping runs on that full rel path, not the bare
    filename, so a keep-mode nested multi-disc source (CD1/01.flac,
    CD2/01.flac) never collides just because two discs share a basename.
    """
    if scheme == "custom":
        tmpl = template
        if not tmpl:
            raise TemplateError("a custom scheme requires a template")
    else:
        tmpl = PRESETS.get(scheme)
        if tmpl is None:
            raise TemplateError(f"unknown naming scheme: {scheme}")

    parsed = parse_template(tmpl)
    tracks = sorted(recording.tracks, key=lambda t: (t.track_number is None, t.track_number))
    performance = recording.performance
    artist  = performance.artist if performance else None
    venue   = performance.venue  if performance else None
    flatten = flattens(scheme, template)

    names = []
    for t in tracks:
        ctx  = naming_context(recording, performance, artist, venue, t, tracks)
        stem = render(parsed, ctx)
        ext  = os.path.splitext(t.file_path or "")[1] or ".flac"
        name = stem + ext
        if not flatten:
            current_dir = os.path.dirname(t.file_path or "")
            if current_dir:
                name = f"{current_dir}/{name}"
        names.append(name)

    folder_rel = getattr(recording, "folder_path", "") or ""
    names = [fit_name(n, folder_rel) for n in names]
    names = _dedupe_names(names)
    return list(zip(tracks, (t.file_path for t in tracks), names))
