"""
app/utils/resolve.py -- The one field resolver for ingest.

Ingest Field Resolver Design Spec v1 (2026-09-28). Every ingest path -- the
Add Recording wizard, Batch Import, and the Bulk Ingest worker -- runs its
scan payload through resolve() instead of keeping its own merge rules, so a
folder resolves identically no matter which door it came through.

Pure: no network, no filesystem writes. find_duplicates() is the one
read-only DB touch point, kept separate so everything else here stays
testable with nothing but a scan payload.
"""
import unicodedata
from dataclasses import dataclass, field as _dc_field

from app.utils.ingest import (
    FOLDER_DATE_RE, _parse_location, title_case, detect_track_flags,
    venue_plausible,
)
from app.utils.venues import is_placeholder_venue_name
from app.utils.bulk_ingest import _folder_tree_artist

# Fingerprint types cheap enough to double as a content-identity key for
# dedup (a header read, not a full re-hash). Canonical home for this
# constant (spec section 6) -- bulk_ingest_run.py imports it from here now
# instead of keeping its own copy. "md5" is deliberately excluded: it
# covers the whole file including encoding, so two different recordings of
# the same show never share one.
DEDUP_FP_TYPES = ("ffp", "st5")


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
            if isinstance(v, list):
                # A multi-valued tag (two ARTIST comments) keeps its first value, the same
                # rule _first_tag() in ingest.py applies to the container fields.
                v = next((str(x).strip() for x in v if str(x).strip()), None)
            if v:
                break
        values.append(v)
    return values or None


def _consistent_tag(tracks, *keys):
    """The tag value shared by EVERY track, or None on any mismatch/absence.

    Moved here from bulk_ingest.py (2026-09-28) -- resolve() is the only
    remaining caller (album/artist rules), so this is its single home now.
    """
    values = _tag_container_values(tracks, *keys)
    if not values or not values[0]:
        return None
    return values[0] if all(v == values[0] for v in values) else None


# ── Field / Resolved ─────────────────────────────────────────────────────────

@dataclass
class Field:
    """One resolved metadata field.

    value        the winning value (None if nothing offered one)
    source       "tags" | "info" | "folder" | None -- which source won
                 ("applied" for a value a person typed: locked, never re-resolved)
    candidates   {source: value} for every source that offered a value, so a
                 UI can show "tags say X, info file says Y" without
                 re-deriving anything
    conflict     True when two sources both state something for this field
                 and disagree
    confidence   "confident" | "tentative" | "empty" (reader/confidence.py). A field
                 built without an assessment (a value a person typed, or a field the
                 resolver reads as stated) is confident when it has a value
    evidence     [{source, text, line, role, extractor, score, notes}] -- where the value
                 was read from, for the Resolver pane. line is a physical line index
                 into the info text (0-based) or None
    runner_up    {value, source, text, line, score} when a rival came close, else None
    p, margin, logit
                 calibrated probability, margin over the best rival and the raw logit
                 the confidence came from (None when not assessed)
    """
    value: object = None
    source: str = None
    candidates: dict = _dc_field(default_factory=dict)
    conflict: bool = False
    confidence: str = None
    evidence: list = _dc_field(default_factory=list)
    runner_up: dict = None
    p: float = None
    margin: float = None
    logit: float = None

    def __post_init__(self):
        if self.confidence is None:
            self.confidence = "confident" if self.value is not None else "empty"

    def to_dict(self):
        return {"value": self.value, "source": self.source,
                "candidates": dict(self.candidates), "conflict": self.conflict,
                "confidence": self.confidence, "evidence": [dict(e) for e in self.evidence],
                "runner_up": dict(self.runner_up) if self.runner_up else None,
                "p": None if self.p is None else round(self.p, 4),
                "margin": None if self.margin is None else round(self.margin, 2),
                "logit": None if self.logit is None else round(self.logit, 2)}


@dataclass
class Duplicate:
    """One duplicate hit. kind is 'folder' | 'content' | 'show'."""
    kind: str
    recording_id: int

    def to_dict(self):
        return {"kind": self.kind, "recording_id": self.recording_id}


@dataclass
class Resolved:
    date: Field
    artist: Field
    venue: Field
    event: Field
    stage: Field
    city: Field
    state: Field
    country: Field
    source: Field
    lineage: Field
    source_tag: Field
    shnid: Field
    album: Field
    kind: str
    tracks: list
    folder_path: str = None
    scan: dict = None
    duplicates: list = _dc_field(default_factory=list)
    status: str = None
    reasons: list = _dc_field(default_factory=list)
    members: list = _dc_field(default_factory=list)   # the person names the billing lists (never saved unattended)

    def to_dict(self):
        return {
            "date":        self.date.to_dict(),
            "artist":      self.artist.to_dict(),
            "venue":       self.venue.to_dict(),
            "event":       self.event.to_dict(),
            "stage":       self.stage.to_dict(),
            "city":        self.city.to_dict(),
            "state":       self.state.to_dict(),
            "country":     self.country.to_dict(),
            "source":      self.source.to_dict(),
            "lineage":     self.lineage.to_dict(),
            "source_tag":  self.source_tag.to_dict(),
            "shnid":       self.shnid.to_dict(),
            "album":       self.album.to_dict(),
            "kind":        self.kind,
            "tracks":      list(self.tracks),
            "duplicates":  [d.to_dict() for d in self.duplicates],
            "status":      self.status,
            "reasons":     list(self.reasons),
            "members":     list(self.members),
        }


# ── Evidence and confidence (reader/confidence.py) ───────────────────────────

from app.utils.reader import confidence as _conf
from app.utils.reader.library import norm_key as _nkey


def _row(source, text=None, line=None, role=None, extractor=None, score=None, notes=""):
    """One evidence row. line is a physical line index into the info text, or None."""
    return {"source": source, "text": text, "line": line, "role": role, "extractor": extractor,
            "score": None if score is None else round(float(score), 2), "notes": notes or ""}


def _template(scan):
    """The folder name read back as Artist - Date - Venue - Location, or {}."""
    from app.utils.reader.folder import parse_template
    return parse_template(scan.get("folder_name")) or {}


def _template_group(groups, fname, scan, text):
    """Corroboration from a template-named folder: adds its score to the group that
    says the same thing, or becomes a rival group when it says something else."""
    if text:
        groups.add(_nkey(text), _conf.FOLDER_TEMPLATE_SCORE[fname], text,
                   [_row("folder", text, None, None, "folder name", _conf.FOLDER_TEMPLATE_SCORE[fname], "template")])


def _info_evidence(from_info):
    return (from_info or {}).get("evidence") or {}


def _agree_src(row):
    """Which independent source a row stands for, or None when it does not count as one
    (the library and Atlas rows shown under an info candidate are part of its score)."""
    if not row:
        return None
    src = row.get("source")
    if src == "library" and row.get("independent"):
        return "library"                     # an act a person confirmed, matched exactly
    if src == "atlas" and row.get("independent"):
        return "atlas"                       # the show index: an act listed on this very day
    if src == "folder" and row.get("extractor") == "folder tree":
        return "parent"                      # the artist folder it was filed under
    return src if src in _conf.INDEPENDENT_SOURCES else None


class _Groups:
    """Candidates of one field, pooled by what they say: key -> {score, value, rows}.
    Sources that say the same thing add their scores (agreement is evidence the text
    was read right); a candidate that says something else competes with it. Two or
    more independent sources saying it earn the field's agreement bonus on top."""

    def __init__(self, field=None):
        self.field = field
        self.d = {}

    def add(self, key, score, value, rows, agree=True):
        g = self.d.setdefault(key, {"score": 0.0, "value": value, "rows": [], "srcs": set()})
        g["score"] += score
        g["rows"].extend(rows)
        src = _agree_src(rows[0]) if rows and agree else None
        if src:
            g["srcs"].add(src)

    def bonus(self, g):
        extra = min(max(len(g["srcs"]) - 1, 0), _conf.AGREE_CAP)
        return _conf.AGREE_BONUS.get(self.field, 0.0) * extra

    def scores(self):
        return {k: g["score"] + self.bonus(g) for k, g in self.d.items()}


def _runner_up(group):
    if not group:
        return None
    r0 = group["rows"][0] if group["rows"] else {}
    return {"value": group["value"], "source": r0.get("source"), "text": r0.get("text"),
            "line": r0.get("line"), "score": round(group["score"], 2)}


def _assessed(fname, groups, chosen_key, **field_kwargs):
    """Build the Field for `fname`: confidence, evidence rows of the chosen candidate and
    the runner-up when it is close. A field whose NONE candidate wins comes back empty."""
    a = _conf.assess(fname, groups.scores(), chosen_key)
    if a.none_wins:
        return Field()
    ru = None
    if a.runner_key is not None and a.margin < _conf.CLOSE_MARGIN:
        ru = _runner_up(groups.d.get(a.runner_key))
    g = groups.d[chosen_key]
    rows = list(g["rows"])
    if groups.bonus(g) and rows:
        rows[0] = dict(rows[0], notes=(rows[0]["notes"] + "; " if rows[0]["notes"] else "")
                       + "agreed by " + ", ".join(sorted(g["srcs"])) + f" (+{groups.bonus(g):g})")
    return Field(confidence=a.confidence, evidence=rows,
                 runner_up=ru, p=a.p, margin=a.margin, logit=a.logit, **field_kwargs)


def _decoded_rows(c, source="info"):
    """Evidence rows for one decoder candidate: the info text row, then one row each for
    the library and Atlas features that raised its score (shown, not added again)."""
    rows = [_row(source, c.get("text"), c.get("line"), c.get("role"), "decoder", c.get("score"),
                 "runner-up role " + str((c.get("runner_up") or {}).get("value") or "none"))]
    for name, contrib in c.get("ext") or []:
        rows.append(_row("library" if name.startswith("lib_") else "atlas", c.get("text"),
                         c.get("line"), c.get("role"),
                         "library" if name.startswith("lib_") else "atlas", contrib,
                         f"part of the info score ({name})"))
    return rows


def _add_info_groups(groups, fname, info_value, from_info, key=_nkey):
    """Add the info text's candidates for a decoder field. Returns the chosen key."""
    ev = _info_evidence(from_info)
    chosen_key = key(info_value)
    cl = (ev.get("cands") or {}).get(fname)
    chosen_seen = False
    for c in cl or []:
        if c.get("chosen") and not chosen_seen:
            chosen_seen = True
            groups.add(chosen_key, c["score"], info_value, _decoded_rows(c))
        elif not c.get("chosen"):
            k = key(c.get("text"))
            if k != chosen_key:
                groups.add(k, c["score"], c.get("text"), _decoded_rows(c))
    if not chosen_seen:
        f = (ev.get("fields") or {}).get(fname) or {}
        score = f.get("score", _conf.INFO_NO_EVIDENCE_SCORE)
        groups.add(chosen_key, score, info_value,
                   [_row("info", f.get("text") or info_value, f.get("line"), f.get("role"),
                         "decoder" if f else None, score, f.get("via") or "")])
    return chosen_key


# ── Date ──────────────────────────────────────────────────────────────────

import re as _re

_TAG_DATE_RE = _re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")

_DATE_SOURCE_PRIORITY = {"tags": 0, "info": 1, "folder": 2}


def _date_from_tags(from_tags):
    raw = (from_tags.get("concert_date") or "").strip()
    m = _TAG_DATE_RE.match(raw)
    if not m or not m.group(1):
        return None
    return (int(m.group(1)),
            int(m.group(2)) if m.group(2) else None,
            int(m.group(3)) if m.group(3) else None)


def _date_from_info(from_info):
    y = from_info.get("year")
    if not y:
        return None
    return (y, from_info.get("month"), from_info.get("day"))


def _date_from_folder(folder_name):
    """The one full date written in the folder name, in any form the date grammar reads
    (1997-11-22, 11-22-97, Nov 22 1997 ...). A transfer date, a range, an ambiguous
    reading, or two different full dates give nothing."""
    from app.utils.reader.dates import find_dates
    seen = set()
    for m in find_dates(folder_name or ""):
        if m.role_hint == "transfer" or m.ambiguous or m.end_day:
            continue
        if m.year and m.month and m.day:
            seen.add((m.year, m.month, m.day))
    return seen.pop() if len(seen) == 1 else None


def _date_precision(v):
    return sum(1 for c in v if c is not None)


def _date_compatible(a, b):
    """No component both state differs."""
    return all(x is None or y is None or x == y for x, y in zip(a, b))


def _date_text(v):
    return "-".join(f"{c:02d}" if i else str(c) for i, c in enumerate(v) if c is not None)


def _date_score_groups(cands, scan, from_info):
    """Pool the sources' dates into groups. A date's score is the weight of every source
    stating it, plus a fraction of the weight of each source stating a less precise date
    inside it. Returns (groups, rows_by_tuple)."""
    ev = (_info_evidence(from_info).get("date") or {})
    folder_name = scan.get("folder_name", "")
    groups = _Groups("date")
    tuples = sorted(set(cands.values()), key=lambda t: tuple(c or 0 for c in t))
    for t in tuples:
        for src, v in cands.items():
            w = _conf.DATE_WEIGHT[src]
            if v == t:
                full = True
            elif _date_precision(v) < _date_precision(t) and _date_compatible(v, t):
                full = False
            else:
                continue
            score = w if full else w * _conf.DATE_PARTIAL
            rows = []
            if src == "info":
                lead = ev.get("lead") or {}
                rows.append(_row("info", lead.get("text") or _date_text(v), lead.get("line"), "DATE",
                                 "date grammar", score, lead.get("role_hint") or ""))
                if full:
                    sup = [x for x in ev.get("support") or []]
                    extra = min(_conf.DATE_SUPPORT_CAP, _conf.DATE_SUPPORT_EACH * len(sup))
                    score += extra
                    for x in sup:
                        rows.append(_row("info", x.get("text"), x.get("line"), "DATE", "date grammar",
                                         _conf.DATE_SUPPORT_EACH, "agrees"))
                    against = ev.get("against") or []
                    score -= min(_conf.DATE_AGAINST_CAP, _conf.DATE_AGAINST_EACH * sum(
                        1 for x in against if sum(c is not None for c in x["date"]) >= 2))
            elif src == "folder":
                rows.append(_row("folder", _date_text(v), None, None, "folder name", score, ""))
            else:
                rows.append(_row("tags", _date_text(v), None, None, "tag", score, ""))
            groups.add(t, score, v, rows, agree=full)
    return groups


def _resolve_date_field(scan):
    from_tags = scan["suggestions"]["from_tags"]
    from_info = scan["suggestions"]["from_info_file"]
    folder_name = scan.get("folder_name", "")

    cands = {}
    t = _date_from_tags(from_tags)
    if t:
        cands["tags"] = t
    i = _date_from_info(from_info)
    if i:
        cands["info"] = i
    f = _date_from_folder(folder_name)
    if f:
        cands["folder"] = f

    if not cands:
        return Field()

    # Conflict: any two candidates both state a component and it differs.
    # "1979" vs "1979-06-14" is agreement -- only a component BOTH sources
    # give gets compared.
    conflict = False
    items = list(cands.items())
    for a in range(len(items)):
        for b in range(a + 1, len(items)):
            va, vb = items[a][1], items[b][1]
            for ca, cb in zip(va, vb):
                if ca is not None and cb is not None and ca != cb:
                    conflict = True

    best_source = min(cands, key=lambda s: (-_date_precision(cands[s]),
                                             _DATE_SOURCE_PRIORITY[s]))
    value = cands[best_source]

    def _as_dict(v):
        return {"year": v[0], "month": v[1], "day": v[2]}

    # Confidence: the chosen date against every date that contradicts it (a less precise
    # date inside it is not a rival) and NONE.
    groups = _date_score_groups(cands, scan, from_info)
    rivals = {k: g for k, g in groups.d.items()
              if k == value or not (_date_compatible(k, value))}
    groups.d = rivals
    fld = _assessed("date", groups, value, value=_as_dict(value), source=best_source,
                    candidates={s: _as_dict(v) for s, v in cands.items()}, conflict=conflict)
    if fld.runner_up and isinstance(fld.runner_up.get("value"), tuple):
        fld.runner_up["value"] = _as_dict(fld.runner_up["value"])
    return fld


# ── Artist ────────────────────────────────────────────────────────────────

def _act_key_norm(name):
    """Same normalisation resolve_similar_artist_ids() uses -- imported
    lazily to avoid a hard dependency on the DB-touching artists module at
    import time (this file must stay importable with no app context)."""
    from app.utils.artists import _act_key
    return _act_key(name)


# A free-form folder name beside the info-text artist (reader/folder.py). Two switches, each
# measured on G1 (2026-10-05):
#   FOLDER_LEAD_AGREES   the folder's lead (the text before its first date or separator) IS the
#                        info reading: a full second source, scored like a template folder.
#                        A folder that only CONTAINS the reading earns the +3 agreement bonus.
#   FOLDER_LEAD_EXTENDS  the lead begins with the reading and continues as a billing ("& Joe
#                        Pass", ", Herbie Hancock", "Quartet"): propose the lead, tentative.
FOLDER_LEAD_AGREES = True
FOLDER_LEAD_EXTENDS = True
# The lead is the reading without its trailing group word ("Miles Davis" for "Miles Davis
# Septet"): a billing variant, so the folder still agrees. Score it like an exact lead
# (FOLDER_TEMPLATE_SCORE) or, at 0.0, with only the agreement bonus.
FOLDER_LEAD_VARIANT_SCORE = None            # None: the template score
# Cross-checks between the sources (2026-10-06), each switchable and measured on G1/G2/G3:
#   FOLDER_LEAD_COMPETES  the lead is a known act and the info reading matches no act at all:
#                         the lead is the artist (tentative)
#   SHOW_INDEX_ARTIST     the Atlas lists the chosen act on the resolved date: an independent source
#   THREE_WAY_VOTE        tags, folder lead and info text: the two that agree win
FOLDER_LEAD_COMPETES = True
SHOW_INDEX_ARTIST = True
THREE_WAY_VOTE = True

_EXT_BILLING = _re.compile(r"^\s*(?:&|\+|,|and\b)\s*\S", _re.I)
_PART_SPLIT = _re.compile(r"\s*(?:,|&|\+|\band\b)\s*", _re.I)
# Words a folder lead can be without naming an act.
_GENERIC_LEAD = {"various", "various artists", "va", "unknown", "unknown artist", "concert", "show", "live", "master",
                 "taper", "bootleg", "rehearsal", "soundcheck", "sound check", "sbd", "aud", "mtx", "fm", "matrix",
                 "audience", "soundboard", "radio", "tv", "broadcast", "demo", "interview", "jam", "set", "disc",
                 "cd", "flac", "mp3", "shn", "wav", "unreleased", "bonus", "outtakes", "misc", "new folder"}


def _known_place_or_event(text, library, atlas):
    """True when `text` is a venue, event or place the library or the Atlas knows by exactly that
    name, or reads like a festival."""
    from app.utils.reader.features import is_festival_like
    if is_festival_like(text):
        return True
    try:
        if library is None:
            from app.utils.reader.library import current_library
            library = current_library()
        if library.venue_match(text) or library.event_match(text):
            return True
    except Exception:           # noqa: BLE001
        pass
    try:
        if atlas is None:
            from app.atlas.lookup import current_atlas
            atlas = current_atlas()
        if atlas is not None and (atlas.venue(text, fuzzy=False, limit=1) or atlas.area(text, limit=1)
                                  or atlas.event(text, fuzzy=False, limit=1)):
            return True
    except Exception:           # noqa: BLE001
        pass
    return False


def _is_known_act(text, library, atlas):
    """An exact library act name (or learned alias), or an exact Atlas act."""
    try:
        if library is None:
            from app.utils.reader.library import current_library
            library = current_library()
        m = library.artist_match(text)
        if m and m[1] == "exact":
            return True
    except Exception:           # noqa: BLE001
        pass
    try:
        if atlas is None:
            from app.atlas.lookup import current_atlas
            atlas = current_atlas()
        if atlas is not None and atlas.artist(text, fuzzy=False, limit=1):
            return True
    except Exception:           # noqa: BLE001
        pass
    return False


def _lead_is_not_an_act(lead, library=None, atlas=None):
    """A folder lead that cannot be an artist's name: a generic word, a festival, or a known
    place or event -- unless it is also a known act ("Boston", "Chicago", "Rush", "Santana"),
    which a place or event of the same name does not block. A venue the Atlas knows only by a
    near name ("Red Rocks") is caught by a fuzzy venue lookup."""
    from app.utils.reader.features import is_festival_like
    k = _nkey(lead)
    if not k or k in _GENERIC_LEAD or all(w in _GENERIC_LEAD for w in k.split()):
        return True
    if is_festival_like(lead):
        return True
    if _is_known_act(lead, library, atlas):
        return False
    if _known_place_or_event(lead, library, atlas):
        return True
    try:
        if atlas is None:
            from app.atlas.lookup import current_atlas
            atlas = current_atlas()
        if atlas is None:
            return False
        if atlas.venue(lead, limit=1):                                       # fuzzy / alias
            return True
        if len(lead.split()) >= 2:
            # "Red Rocks": the start of a venue name that goes on with a building word
            from app.utils.reader.features import VENUE_WORDS
            nl = _nkey(lead) + " "
            for c in atlas.venue(lead, limit=5, min_score=0.5):
                nc = _nkey(c.name)
                if nc.startswith(nl) and set(nc[len(nl):].split()) & set(VENUE_WORDS):
                    return True
        return False
    except Exception:           # noqa: BLE001
        return False


def _glued_shortcode(folder_name, lead):
    """"ronnie2015-05-01": the lead runs straight into digits. A shortcode a taper typed, not an
    independent statement of the act."""
    t = (folder_name or "").strip()
    return bool(lead) and t.lower().startswith(lead.lower()) and t[len(lead):len(lead) + 1].isdigit()


def _looks_like_act_part(part, library=None, atlas=None):
    """One extra name in a folder billing ("Joe Pass", "the New South"): not a place, venue or
    festival, no digits, and not a lone word unless it is a group word."""
    words = part.split()
    if not words or _re.search(r"\d", part):
        return False
    if len(words) == 1 and words[0].lower().strip(".") not in _GROUP_WORDS_ALL:
        return False
    from app.utils.reader.features import VENUE_WORDS
    if {w.lower().strip(".,") for w in words} & set(VENUE_WORDS):
        return False                          # "Chicago Theatre": a building word
    return not _known_place_or_event(part, library, atlas)


_BAND_PREFIX = {"big", "jazz", "swing"}


def _without_group_tail(name):
    """"Miles Davis Septet" -> "Miles Davis", "Count Basie Big Band" -> "Count Basie"; the
    name itself when it does not end in a group word (or would be left empty)."""
    words = name.split()
    if len(words) >= 2 and words[-1].lower().strip(".") in _GROUP_WORDS_ALL:
        words = words[:-1]
        if len(words) >= 2 and words[-1].lower() in _BAND_PREFIX:
            words = words[:-1]
    return " ".join(words)


def _lead_is_variant_of(lead, reading):
    """The reading is the lead plus a trailing group word."""
    base = _without_group_tail(reading)
    return base != reading and bool(base) and _nkey(base) == _nkey(lead)


def _cur_atlas():
    try:
        from app.atlas.lookup import current_atlas
        return current_atlas()
    except Exception:           # noqa: BLE001
        return None


def _cur_library():
    try:
        from app.utils.reader.library import current_library
        return current_library()
    except Exception:           # noqa: BLE001
        return None


def _matches_any_act(text, library=None, atlas=None):
    """The text is, or reads as the core of, an act the library or the Atlas knows."""
    try:
        m = (library or _cur_library()).artist_match(text)
        if m:
            return True
    except Exception:           # noqa: BLE001
        pass
    try:
        atlas = atlas or _cur_atlas()
        if atlas is not None and atlas.artist(text, fuzzy=False, limit=1):
            return True
    except Exception:           # noqa: BLE001
        pass
    return False


def _is_known_act_strict(text, library=None, atlas=None):
    """An exact library act, or an exact Atlas act (a one-word name needs some MusicBrainz history,
    as in the decoder)."""
    try:
        m = (library or _cur_library()).artist_match(text)
        if m and m[1] == "exact":
            return True
    except Exception:           # noqa: BLE001
        pass
    try:
        atlas = atlas or _cur_atlas()
        if atlas is None:
            return False
        for c in atlas.artist(text, fuzzy=False, limit=3):
            if c.how in ("exact", "squashed") and (len(text.split()) > 1 or (c.extra.get("popularity") or 0) >= 3):
                return True
    except Exception:           # noqa: BLE001
        pass
    return False


def _lead_agrees(lead, reading):
    """The lead and the reading name the same act: equal, or one is the other plus a group word."""
    return bool(lead and reading) and (_nkey(lead) == _nkey(reading) or _lead_is_variant_of(lead, reading)
                                       or _lead_is_variant_of(reading, lead))


def _folder_lead_act(scan, library=None, atlas=None):
    """The folder's lead when it can be an act's name (passes the not-an-act guard, is not a shortcode
    glued to digits), else None."""
    from app.utils.reader.folder import MIN_NAME_KEY, folder_lead
    name = scan.get("folder_name")
    lead = folder_lead(name)
    if (not lead or len(_nkey(lead)) < MIN_NAME_KEY or _glued_shortcode(name, lead)
            or _lead_is_not_an_act(lead, library, atlas)):
        return None
    return lead


def _full_date(date_f):
    v = getattr(date_f, "value", None) or {}
    if v.get("year") and v.get("month") and v.get("day"):
        return (int(v["year"]), int(v["month"]), int(v["day"]))
    return None


def _shows_listed(atlas, act, full, memo):
    """The Atlas's exact-day shows of `act` on `full` (y, m, d): the show index, real outside evidence."""
    if atlas is None or not act or not full:
        return []
    k = (_nkey(act), full)
    if k not in memo:
        try:
            memo[k] = [e for e in atlas.event_place(act, full) if e.get("exact")]
        except Exception:           # noqa: BLE001
            memo[k] = []
    return memo[k]


def _lead_remainder(lead, reading):
    """What `lead` says after the artist `reading` (matched on norm_key), or None."""
    nr = _nkey(reading)
    for k in range(1, len(lead)):
        if (lead[k] in " ,&+") and _nkey(lead[:k]) == nr:
            return lead[k:]
    return None


def _free_form_folder_source(groups, reading, cased_reading, folder_name, templated=False,
                             library=None, atlas=None, shown=None, swap=None):
    """Add the folder-name evidence for the info artist `reading` to `groups`. Returns the
    folder's billing when the extension proposes it in place of the reading, else None.
    `shown` (a list) receives the lead when it names an act the reading does not agree with
    or extend: display only, it adds no score. `swap` (a list; None switches the rule off) gets
    True when the lead is a known act and the reading matches no act at all: the lead is returned
    as the artist."""
    from app.utils.reader.folder import MIN_NAME_KEY, folder_lead, names_artist
    nk = _nkey(reading)
    lead = folder_lead(folder_name)
    if (lead and not templated and FOLDER_LEAD_AGREES and len(nk) >= MIN_NAME_KEY
            and _nkey(lead) != nk and len(_nkey(lead)) >= MIN_NAME_KEY
            and _lead_is_variant_of(lead, reading)
            and not _lead_is_not_an_act(lead, library, atlas)):
        sc = _conf.FOLDER_TEMPLATE_SCORE["artist"] if FOLDER_LEAD_VARIANT_SCORE is None else FOLDER_LEAD_VARIANT_SCORE
        if _glued_shortcode(folder_name, lead):
            sc = 0.0
        groups.add(nk, sc, cased_reading,
                   [_row("folder", title_case(lead), None, None, "folder name", sc, "lead, without the group word")])
        if shown is not None:
            shown.append(title_case(lead))
        return None
    if lead and not templated and FOLDER_LEAD_AGREES and len(nk) >= MIN_NAME_KEY and _nkey(lead) == nk:
        if _lead_is_not_an_act(lead, library, atlas):
            return None                       # the "artist" is a place, a festival or a generic word
        if not _glued_shortcode(folder_name, lead):
            sc = _conf.FOLDER_TEMPLATE_SCORE["artist"]
            groups.add(nk, sc, cased_reading, [_row("folder", reading, None, None, "folder name", sc, "lead")])
            return None
        # glued shortcode: no more than the contains bonus below
    if not templated and names_artist(folder_name, reading):
        groups.add(nk, 0.0, cased_reading,
                   [_row("folder", reading, None, None, "folder name", 0.0, "free-form")])
    if lead and FOLDER_LEAD_EXTENDS and len(nk) >= MIN_NAME_KEY:
        rest = _lead_remainder(lead, reading)
        if rest and rest.strip():
            words = rest.split()
            if len(words) == 1 and words[0].lower().strip(".") in _GROUP_WORDS_ALL:
                return title_case(lead)
            if _EXT_BILLING.match(rest):
                parts = [x for x in _PART_SPLIT.split(rest) if x.strip()]
                if parts and all(_looks_like_act_part(x, library, atlas) for x in parts):
                    return title_case(lead)
    if (FOLDER_LEAD_COMPETES and swap is not None and lead and len(_nkey(lead)) >= MIN_NAME_KEY
            and _nkey(lead) != nk and not _glued_shortcode(folder_name, lead)
            and (f" {_nkey(lead)} " not in f" {nk} " or nk.startswith(_nkey(lead) + " "))   # "X with <lead>" is its own billing
            and not _matches_any_act(reading, library, atlas)
            and _is_known_act_strict(lead, library, atlas)
            and not _lead_is_not_an_act(lead, library, atlas)):
        swap.append(True)
        return title_case(lead)
    if (shown is not None and lead and not templated and len(_nkey(lead)) >= MIN_NAME_KEY
            and not _nkey(lead) == nk and not names_artist(folder_name, reading)
            and not _glued_shortcode(folder_name, lead)
            and not _lead_is_not_an_act(lead, library, atlas)):
        shown.append(title_case(lead))        # the folder names an act the reading differs from
    return None


def _resolve_artist_field(scan, *, library_root=None, placement=None, date_f=None, memo=None):
    from_tags = scan["suggestions"]["from_tags"]
    from_info = scan["suggestions"]["from_info_file"]
    memo = {} if memo is None else memo

    cands = {}
    tag_artist = _consistent_tag(from_tags.get("tracks", []), "artist", "albumartist")
    if tag_artist:
        cands["tags"] = tag_artist
    if from_info.get("artist"):
        cands["info"] = from_info["artist"]
    folder_artist = None
    if placement == "artist" and library_root and scan.get("folder_path"):
        folder_artist = _folder_tree_artist(scan["folder_path"], library_root)
        if folder_artist:
            cands["folder"] = folder_artist

    if not cands:
        return Field()

    conflict = False
    if "tags" in cands and "info" in cands:
        if _act_key_norm(cands["tags"]) != _act_key_norm(cands["info"]):
            conflict = True

    # Tags, folder lead and info text: when the info text and the folder lead agree and the tags
    # say something else, the two that agree win. The tags still disagree, so the conflict stays.
    vote_info = False
    if THREE_WAY_VOTE and conflict and "folder" not in cands:
        lead = _folder_lead_act(scan)
        if lead and _lead_agrees(lead, cands["info"]) and not _lead_agrees(lead, cands["tags"]):
            vote_info = True

    source = "tags" if ("tags" in cands and not vote_info) else ("info" if "info" in cands else "folder")
    cased = {k: title_case(v) for k, v in cands.items()}

    # Pooled by the exact normalised name, not the act core: "Miles Davis Sextet" does not
    # corroborate "Miles Davis" (the conflict test above still compares act cores).
    groups = _Groups("artist")
    if "tags" in cands:
        groups.add(_nkey(cands["tags"]), _conf.TAGS_SCORE["artist"], cased["tags"],
                   [_row("tags", cands["tags"], None, None, "tag", _conf.TAGS_SCORE["artist"], "")])
    if "info" in cands:
        _add_info_groups(groups, "artist", cased["info"], from_info)
        if _info_evidence(from_info).get("artist_confirmed"):
            # a person already confirmed this act under exactly this name: a source of its own
            # (it adds no score of its own, only the agreement bonus beside the info candidate)
            groups.add(_nkey(cands["info"]), 0.0, cased["info"],
                       [dict(_row("library", cands["info"], None, None, "library", 0.0, ""), independent=True)])
    if "folder" in cands:
        groups.add(_nkey(cands["folder"]), _conf.FOLDER_ARTIST_SCORE, cased["folder"],
                   [_row("folder", cands["folder"], None, None, "folder tree", _conf.FOLDER_ARTIST_SCORE, "")])
    tpl_artist = _template(scan).get("artist")
    _template_group(groups, "artist", scan, tpl_artist)
    extended = None
    shown, swap = [], []
    if "info" in cands and "folder" not in cands:
        extended = _free_form_folder_source(groups, cands["info"], cased["info"], scan.get("folder_name"),
                                            templated=bool(tpl_artist), shown=shown,
                                            swap=None if "tags" in cands else swap)
        if extended:
            chosen = _nkey(extended)
            cased["folder"] = extended
            if not (swap and tpl_artist and _nkey(tpl_artist) == chosen):     # the template row already counts
                groups.add(chosen, _conf.FOLDER_TEMPLATE_SCORE["artist"], extended,
                           [_row("folder", extended, None, None, "folder name",
                                 _conf.FOLDER_TEMPLATE_SCORE["artist"],
                                 "the folder names a known act" if swap else "billing in the folder name")])
    if not extended:
        chosen = _nkey(cased[source])
        if chosen not in groups.d:
            groups.add(chosen, _conf.INFO_NO_EVIDENCE_SCORE, cased[source], [])
    value = extended or cased[source]
    src = "folder" if extended else source

    # The Atlas's show index: it lists this act on this day. Outside evidence, not read from the files.
    corroborated = False
    atlas = _cur_atlas() if (SHOW_INDEX_ARTIST and date_f is not None) else None
    full = _full_date(date_f) if atlas is not None else None
    if full and _shows_listed(atlas, value, full, memo):
        corroborated = True
        sc = _conf.ATLAS_SHOW_SCORE["artist"]
        groups.add(chosen, sc, value,
                   [dict(_row("atlas", value, None, None, "show index", sc,
                              "lists this act on this date"), independent=True)])
    fld = _assessed("artist", groups, chosen, value=value, source=src, candidates=cased, conflict=conflict)
    if extended and fld.confidence == "confident" and not (swap and corroborated):
        fld.confidence = "tentative"          # a billing read off a folder name is never confident alone
    if shown and fld.confidence != "empty" and "folder" not in fld.candidates:
        # Display only, after the score: the Sources popover quotes what the folder named.
        fld.candidates["folder"] = shown[0]
        if not any(e.get("source") == "folder" for e in fld.evidence):
            fld.evidence = list(fld.evidence) + [
                _row("folder", shown[0], None, None, "folder name", 0.0, "names a different billing")]
    return fld


# ── Venue + city/state/country ───────────────────────────────────────────

def _show_place_row(groups, chosen, value, field, shows, atlas):
    """The show index names this place for the act on this day: an independent source for the
    venue (the Atlas place's names) or the city."""
    if not shows or atlas is None or chosen not in groups.d:
        return
    hit = False
    for e in shows:
        if field == "city":
            hit = hit or (bool(e.get("city")) and _nkey(e["city"]) == chosen)
        elif e.get("place_id") is not None:
            try:
                hit = hit or chosen in {_nkey(k) for k in atlas.place_keys(e["place_id"])}
            except Exception:           # noqa: BLE001
                pass
    if hit:
        sc = _conf.ATLAS_SHOW_SCORE[field]
        groups.add(chosen, sc, value, [dict(_row("atlas", value, None, None, "show index", sc,
                                                 "lists this act here on this date"), independent=True)])


def _resolve_venue_field(scan, shows=None, atlas=None):
    from_tags = scan["suggestions"]["from_tags"]
    from_info = scan["suggestions"]["from_info_file"]

    cands = {}
    tv = (from_tags.get("venue") or "").strip()
    if tv and not is_placeholder_venue_name(tv) and venue_plausible(tv):
        cands["tags"] = tv
    iv = (from_info.get("venue") or "").strip()
    if iv and not is_placeholder_venue_name(iv) and venue_plausible(iv):
        cands["info"] = iv

    if not cands:
        return Field()

    source = "tags" if "tags" in cands else "info"
    groups = _Groups("venue")
    if "tags" in cands:
        groups.add(_nkey(cands["tags"]), _conf.TAGS_SCORE["venue"], cands["tags"],
                   [_row("tags", cands["tags"], None, None, "tag", _conf.TAGS_SCORE["venue"], "")])
    if "info" in cands:
        _add_info_groups(groups, "venue", cands["info"], from_info)
    _template_group(groups, "venue", scan, _template(scan).get("venue"))
    chosen = _nkey(cands[source])
    _show_place_row(groups, chosen, cands[source], "venue", shows, atlas)
    return _assessed("venue", groups, chosen, value=cands[source], source=source,
                     candidates=dict(cands), conflict=False)


_STAGE_TAIL = _re.compile(r"\s+(?:\S+\s+)?stage$|\s+(?:down|up)stairs$|\s+(?:main|big|small)\s+room$", _re.I)


_NOT_FESTIVAL_NAME_WORDS = {"greatest", "hits", "best", "radio", "song", "songs", "album", "albums", "collection",
                            "special", "tribute", "unplugged", "session", "sessions", "anthology", "essential",
                            "classics", "remastered", "remaster", "reunion", "tour", "live"}


def _festival_is_head(text):
    """True when the festival word is the head noun of `text`: its last token, or the last one
    before "of" ("Festival of the Sun"). "Festivals Greatest Hits" and "Radio Festival Special"
    are not."""
    from app.utils.reader.features import _EVENT_SUFFIX, _EVENT_TOKENS
    toks = [t.lower().strip(".,;:()") for t in text.split()]
    if "of" in toks[1:]:
        toks = toks[:toks.index("of", 1)]
    if set(toks[:-1]) & _NOT_FESTIVAL_NAME_WORDS:
        return False                          # "Greatest Hits Festival": a record, not a festival name
    return bool(toks) and (toks[-1] in _EVENT_TOKENS or bool(_EVENT_SUFFIX.match(toks[-1])))


def _event_from_folder(scan, from_info):
    """The info text gave no event: a free-form folder name that opens with the artist and goes
    on to name a festival ("Go Kurosawa Big Ears Festival (Barley's Tap Room) 3-28-26") gives
    one. Festivals and single events only (event_names rules), the festival word its head noun;
    a leading "live at" / "at" and a trailing stage phrase are dropped. Always tentative."""
    from app.utils.event_names import clean_event_name
    from app.utils.reader.features import counts_as_event, is_festival_like
    from app.utils.reader.folder import folder_lead
    artist = (from_info or {}).get("artist")
    lead = folder_lead(scan.get("folder_name"))
    rest = _lead_remainder(lead, artist) if lead and artist else None
    if rest and _EXT_BILLING.match(rest):
        return Field()                        # "& Friends ...": a longer billing, not an event
    rest = (rest or "").strip(" -,;")
    rest = _re.sub(r"^(?:live\s+)?at\s+", "", rest, flags=_re.I)
    rest = _STAGE_TAIL.sub("", rest).strip(" -,;")
    if not rest or not is_festival_like(rest) or not counts_as_event(rest) or not _festival_is_head(rest):
        return Field()
    ev = clean_event_name(title_case(rest))
    if not ev:
        return Field()
    return Field(value=ev, source="folder", candidates={"folder": ev}, confidence="tentative",
                 evidence=[_row("folder", rest, None, None, "folder name", None, "after the artist")])


def _resolve_info_only_field(scan, key, folder_event=True):
    """event / stage: what the info file's header states. Tags carry neither, and the
    folder name is not read for them, so there is one source and no conflict to track.
    The festival is an Event, "Harbor Stage" is the performance's Stage; neither is
    ever the venue (the reader already keeps them apart)."""
    from_info = scan["suggestions"]["from_info_file"]
    v = from_info.get(key)
    v = v.strip() if isinstance(v, str) else v
    if key == "event":
        from app.utils.event_names import clean_event_name
        v = clean_event_name(v)
    if not v and key == "event":
        return _event_from_folder(scan, from_info) if folder_event else Field()
    if not v:
        return Field()
    groups = _Groups(key)
    chosen = _add_info_groups(groups, key, v, from_info)
    return _assessed(key, groups, chosen, value=v, source="info",
                     candidates={"info": v}, conflict=False)


def _location_score(key, pe):
    how = (pe or {}).get("how")
    if key == "city":
        conf = pe.get("confidence") or "" if how == "line" else ""
        return _conf.CITY_SCORE.get((how, conf), _conf.INFO_NO_EVIDENCE_SCORE)
    table = _conf.STATE_SCORE if key == "state" else _conf.COUNTRY_SCORE
    return table.get(how, _conf.INFO_NO_EVIDENCE_SCORE)


def _resolve_location_fields(scan, venue_field, shows=None, atlas=None):
    """city/state/country: taken from the SAME source as the venue where
    that source has them; filled from the other only when it has none."""
    from_tags = scan["suggestions"]["from_tags"]
    from_info = scan["suggestions"]["from_info_file"]
    sources = {"tags": from_tags, "info": from_info}
    primary = venue_field.source
    place = _info_evidence(from_info).get("place") or {}

    out = {}
    for key in ("city", "state", "country"):
        cands = {}
        for s, d in sources.items():
            v = (d.get(key) or "").strip() if isinstance(d.get(key), str) else d.get(key)
            if v:
                cands[s] = v
        if not cands:
            out[key] = Field()
            continue
        source = primary if primary in cands else ("tags" if "tags" in cands else "info")
        groups = _Groups(key)
        if "tags" in cands:
            groups.add(_nkey(cands["tags"]), _conf.TAGS_SCORE[key], cands["tags"],
                       [_row("tags", cands["tags"], None, None, "tag", _conf.TAGS_SCORE[key], "")])
        if "info" in cands:
            pe = place.get(key)
            score = _location_score(key, pe) if pe else _conf.INFO_NO_EVIDENCE_SCORE
            how = (pe or {}).get("how")
            src = "atlas" if how == "atlas" else "info"
            groups.add(_nkey(cands["info"]), score, cands["info"],
                       [_row(src, (pe or {}).get("text"), (pe or {}).get("line"), "PLACE",
                             "atlas" if how == "atlas" else "place peeler", score, how or "")])
        if key in _conf.FOLDER_TEMPLATE_SCORE:
            _template_group(groups, key, scan, _template(scan).get(key))
        if key == "city":
            _show_place_row(groups, _nkey(cands[source]), cands[source], "city", shows, atlas)
        out[key] = _assessed(key, groups, _nkey(cands[source]), value=cands[source], source=source,
                             candidates=dict(cands), conflict=False)
    return out


# ── Precedence-only fields (source, lineage, source_tag, shnid, album) ──────

def _resolve_precedence_field(scan, key):
    """Tags > info file, unchanged precedence. No conflict tracking -- the
    spec's field-rule table only calls out conflicts for date and artist."""
    from_tags = scan["suggestions"]["from_tags"]
    from_info = scan["suggestions"]["from_info_file"]
    cands = {}
    tv = from_tags.get(key)
    if tv:
        cands["tags"] = tv
    iv = from_info.get(key)
    if iv:
        cands["info"] = iv
    if not cands:
        return Field()
    source = "tags" if "tags" in cands else "info"
    return Field(value=cands[source], source=source, candidates=dict(cands), conflict=False)


def _resolve_folder_only_field(scan, key):
    """source_tag / shnid: folder-name only (neither tags nor the info-file
    parser produce these)."""
    from_info = scan["suggestions"]["from_info_file"]
    v = from_info.get(key)
    if not v:
        return Field()
    return Field(value=v, source="folder", candidates={"folder": v}, conflict=False)


def _resolve_album_field(scan):
    from_tags = scan["suggestions"]["from_tags"]
    v = _consistent_tag(from_tags.get("tracks", []), "album")
    if not v:
        return Field()
    return Field(value=v, source="tags", candidates={"tags": v}, conflict=False)


# ── kind (classify_kind moved here unchanged, spec 3.1) ─────────────────────

def _any_source_has_month(scan):
    from_tags = scan["suggestions"]["from_tags"]
    from_info = scan["suggestions"]["from_info_file"]
    folder_name = scan.get("folder_name", "")
    for v in (_date_from_tags(from_tags), _date_from_info(from_info),
              _date_from_folder(folder_name)):
        if v and v[1]:
            return True
    return False


def classify_kind(scan, album, venue):
    """
    "studio" when ALBUM is identical across every readable file, no source
    (tag, info file, or folder name) states a month anywhere, and no venue
    was found; else "live". Moved here unchanged from app/utils/bulk_ingest.py
    (spec section 3.1) -- same signature, same behaviour.
    """
    no_month_anywhere = (not _any_source_has_month(scan)) if scan else True
    return "studio" if (album is not None and no_month_anywhere and venue is None) else "live"


# ── Tracks (resolve_tracks, spec 3.2) ────────────────────────────────────────

def resolve_tracks(scan):
    """
    Replaces JS buildIngestTracks() and confirm_payload()'s track loop.

    track_number is ALWAYS the scan index (position among audio_files),
    continuous across discs -- never a raw TRACKNUMBER tag, which resets
    per disc. Title prefers the tag title, falling back to the info file's;
    the info track used for that fallback (and for songwriter) is matched
    by POSITION when the info file lists exactly as many tracks as there
    are audio files, and by its own printed number otherwise -- position
    is what survives an info file that restarts numbering per show/disc
    (the Metheny regression: Show 2 restarts at "01").
    """
    from_tags = (scan.get("suggestions") or {}).get("from_tags") or {}
    from_info = (scan.get("suggestions") or {}).get("from_info_file") or {}
    audio_files = scan.get("audio_files") or []
    tag_tracks = from_tags.get("tracks") or []
    info_tracks = from_info.get("tracks") or []

    disc_by_rel, set_by_rel = {}, {}
    for af in audio_files:
        rel = af.get("rel_path") or af.get("filename")
        if not rel:
            continue
        if af.get("set_number"):
            set_by_rel[rel] = af["set_number"]
        if af.get("disc_number") is not None or af.get("disc_track_number") is not None:
            disc_by_rel[rel] = {"disc_number": af.get("disc_number"),
                                "disc_track_number": af.get("disc_track_number")}

    n_audio = len(audio_files)
    align_by_position = bool(info_tracks) and len(info_tracks) == n_audio
    info_by_position = {i + 1: t for i, t in enumerate(info_tracks)}
    info_by_number = {t["number"]: t for t in info_tracks if t.get("number") is not None}

    def info_for(position, printed_number):
        if align_by_position:
            return info_by_position.get(position) or {}
        if printed_number is not None:
            hit = info_by_number.get(printed_number)
            if hit:
                return hit
        return info_by_position.get(position) or {}

    tag_by_rel = {}
    for t in tag_tracks:
        rel = t.get("rel_path") or t.get("filename")
        if rel:
            tag_by_rel[rel] = t

    tracks_out = []
    for i, af in enumerate(audio_files, start=1):
        rel = af.get("rel_path") or af.get("filename") or ""
        tag_t = tag_by_rel.get(rel, {})
        printed = tag_t.get("track_number")
        try:
            printed = int(printed) if printed is not None else None
        except (TypeError, ValueError):
            printed = None
        info_t = info_for(i, printed)

        tag_title = (tag_t.get("title") or "").strip()
        info_title = (info_t.get("title") or "").strip()
        raw_title = tag_title or info_title
        title = title_case(raw_title) if raw_title else f"Track {i}"

        disc = disc_by_rel.get(rel, {})
        tracks_out.append({
            "track_number":       i,
            "title":              title,
            "info_title":         title_case(info_title) if info_title else None,
            "songwriter":         info_t.get("songwriter"),
            "set_number":         set_by_rel.get(rel),
            "disc_number":        disc.get("disc_number"),
            "disc_track_number":  disc.get("disc_track_number"),
            "duration":           tag_t.get("duration"),
            "filename":           rel,
            "flags":              detect_track_flags(title),
        })

    return tracks_out


# ── Members from an "X and Y" billing ────────────────────────────────────

_BILLING_SPLIT = _re.compile(r"\s*(?:,|&|\+|\band\b)\s*", _re.I)
from app.utils.reader.folder import GROUP_WORDS as _GROUP_WORDS
_GROUP_WORDS_ALL = _GROUP_WORDS


def _is_known_band(name, library=None, atlas=None):
    """True when `name` is an act the library or the Atlas knows as a group: a library act with
    that exact name (or a learned alias) that is not also a Musician, or an Atlas act of a kind
    other than Person. A solo act ("Doc Watson") is a person, not a band."""
    try:
        if library is None:
            from app.utils.reader.library import current_library
            library = current_library()
        m = library.artist_match(name)
        if m and m[1] == "exact" and not library.musician_match(name):
            return True
    except Exception:       # noqa: BLE001 -- no library: nothing to reject on
        pass
    try:
        if atlas is None:
            from app.atlas.lookup import current_atlas
            atlas = current_atlas()
        if atlas is not None:
            for c in atlas.artist(name, fuzzy=False, limit=3):
                if (c.extra.get("act_kind") or "person").lower() != "person":
                    return True
    except Exception:       # noqa: BLE001
        pass
    return False


def billing_members(artist, library=None, atlas=None):
    """The person names an artist reading of "X and Y" (or "X, Y, Z and W") states, for
    pre-filling a brand-new act's Members row; [] when the reading is not such a billing.
    Nothing is split when any part starts with "The", ends in a group word ("Band", "Trio",
    "Orchestra"...) or is possessive ("Darol Anger's Fiddle Congress"). A "with ..." sideman
    list is never read here: it is the artist reading itself that is split."""
    if not artist or not isinstance(artist, str):
        return []
    parts = [p.strip() for p in _BILLING_SPLIT.split(artist) if p.strip()]
    if len(parts) < 2:
        return []
    for p in parts:
        words = p.lower().split()
        if words[0] == "the" or words[-1].strip(".") in _GROUP_WORDS or "'s" in p.lower() or "\u2019s" in p.lower():
            return []
    if _re.search(r"\bwith\b|\bfeat", artist, _re.I):
        return []
    if any(_is_known_band(p, library, atlas) for p in parts):
        return []                              # "Los Lobos and Los Lonely Boys": two acts, not people
    out = []
    for p in parts:
        if p.lower() not in (x.lower() for x in out):
            out.append(p)
    return out if len(out) >= 2 else []


# ── resolve() ─────────────────────────────────────────────────────────────

def resolve(scan, *, library_root=None, placement=None):
    """
    scan is build_scan_payload()'s output. Returns a Resolved with every
    field, the track list, kind, and an empty duplicates list (call
    find_duplicates() separately -- it is the one part of this module that
    touches the DB).
    """
    date_f = _resolve_date_field(scan)
    memo = {}
    artist_f = _resolve_artist_field(scan, library_root=library_root, placement=placement, date_f=date_f, memo=memo)
    shows, atlas = None, None
    if SHOW_INDEX_ARTIST and any(e.get("source") == "atlas" and e.get("extractor") == "show index"
                                 for e in artist_f.evidence):
        atlas = _cur_atlas()
        shows = _shows_listed(atlas, artist_f.value, _full_date(date_f), memo)
    venue_f = _resolve_venue_field(scan, shows=shows, atlas=atlas)
    loc = _resolve_location_fields(scan, venue_f, shows=shows, atlas=atlas)
    album_f = _resolve_album_field(scan)
    kind = classify_kind(scan, album_f.value, venue_f.value)
    tracks = resolve_tracks(scan)

    return Resolved(
        date=date_f,
        artist=artist_f,
        venue=venue_f,
        event=_resolve_info_only_field(scan, "event", folder_event=artist_f.source != "folder"),
        stage=_resolve_info_only_field(scan, "stage"),
        city=loc["city"],
        state=loc["state"],
        country=loc["country"],
        source=_resolve_precedence_field(scan, "source"),
        lineage=_resolve_precedence_field(scan, "lineage"),
        source_tag=_resolve_folder_only_field(scan, "source_tag"),
        shnid=_resolve_folder_only_field(scan, "etree_shnid"),
        album=album_f,
        kind=kind,
        tracks=tracks,
        folder_path=scan.get("folder_path"),
        scan=scan,
        members=billing_members(artist_f.value),
    )


# ── Deduplication (spec section 6) ───────────────────────────────────────────

def _norm_rel_path(path):
    p = unicodedata.normalize("NFC", str(path or "")).replace("\\", "/")
    return p.strip("/")


def find_duplicates(resolved, *, library_root=None, hash_cache=None):
    """
    Three of the four dedup mechanisms the spec catalogued as scattered
    (folder identity, content hash, same-show), unified here. Filesystem
    name collision (move_to_library) stays where it is -- that's file
    placement, not catalog dedup.

    hash_cache, when given, is an OPTIONAL {checksum: recording_id} map the
    caller can build once per run (see bulk_ingest_run._build_dedup_map) so
    this does not re-query RecordingFingerprint for every folder. Without
    one, this queries the DB directly.
    """
    from app.extensions import db
    from app.models.recording import Recording, RecordingFingerprint
    from app.models.performance import Performance
    from app.utils.checksums import parse_checksum_file
    from app.utils.artists import resolve_similar_artist_ids

    dups = []
    scan = resolved.scan or {}

    # ── folder identity ──────────────────────────────────────────────────
    folder_path = resolved.folder_path
    if folder_path:
        norm = _norm_rel_path(folder_path)
        rel = norm
        if library_root:
            lib = _norm_rel_path(library_root)
            if lib and norm.startswith(lib + "/"):
                rel = norm[len(lib) + 1:]
        hit = (db.session.query(Recording.id)
               .filter(Recording.folder_path == rel).first())
        if hit:
            dups.append(Duplicate(kind="folder", recording_id=hit[0]))

    # ── content (FFP/ST5 hash) ───────────────────────────────────────────
    fingerprints = scan.get("fingerprints") or []
    my_hashes = set()
    for fp in fingerprints:
        if fp.get("type") not in DEDUP_FP_TYPES:
            continue
        for entry in parse_checksum_file(fp.get("content") or ""):
            my_hashes.add(entry["checksum"])

    if my_hashes:
        owners = {}
        if hash_cache is not None:
            for h in my_hashes:
                rid = hash_cache.get(h)
                if rid is not None:
                    owners[h] = rid
        else:
            rows = (db.session.query(RecordingFingerprint.recording_id,
                                     RecordingFingerprint.content)
                    .filter(RecordingFingerprint.fingerprint_type.in_(DEDUP_FP_TYPES))
                    .all())
            db_map = {}
            for rid, content in rows:
                for entry in parse_checksum_file(content or ""):
                    db_map.setdefault(entry["checksum"], rid)
            for h in my_hashes:
                if h in db_map:
                    owners[h] = db_map[h]

        n_audio = scan.get("audio_file_count") or len(scan.get("audio_files") or [])
        recording_ids = {owners[h] for h in my_hashes if h in owners}
        # Exact duplicate: EVERY track's hash already belongs to ONE existing
        # recording. Partial overlap is not a duplicate and ingests normally
        # (spec section 9, resolved question 1).
        if (n_audio and len(my_hashes) >= n_audio
                and all(h in owners for h in my_hashes) and len(recording_ids) == 1):
            dups.append(Duplicate(kind="content", recording_id=next(iter(recording_ids))))

    # ── same show (informational -- never blocks) ────────────────────────
    artist_name = resolved.artist.value
    date = resolved.date.value or {}
    year = date.get("year")
    if artist_name and year:
        try:
            ids = resolve_similar_artist_ids(artist_name)
            if ids:
                q = db.session.query(Performance).filter(
                    Performance.artist_id.in_(ids), Performance.start_year == year)
                if date.get("month"):
                    q = q.filter(Performance.start_month == date["month"])
                if date.get("day"):
                    q = q.filter(Performance.start_day == date["day"])
                for p in q.all():
                    for rec in (p.recordings or []):
                        dups.append(Duplicate(kind="show", recording_id=rec.id))
        except Exception:  # noqa: BLE001 -- informational only, never fatal
            pass

    return dups


# ── verdict() (spec section 5) ───────────────────────────────────────────────

# Every field this module tracks a conflict for. Kept as an explicit list
# (rather than introspecting Resolved's dataclass fields) so a future field
# that ISN'T conflict-tracked (e.g. one added to Resolved for display only)
# can't silently start emitting conflict reasons nobody asked for.
_CONFLICT_TRACKED_FIELDS = ("date", "artist", "venue", "city", "state",
                           "country", "source", "lineage", "source_tag",
                           "shnid", "album")


def verdict(resolved):
    """
    (status, reasons[]). Replaces bulk_ingest.classify(). `reasons` is a
    list -- one folder can need several things at once.

    Does NOT handle no_audio/unsupported_format/unreadable -- those are
    filesystem-level gates a caller checks before ever building a Resolved
    (see app.api.ingest.auto_confirm), same as classify() left them to
    extract()'s own has_audio/unsupported_only/all_unreadable checks.
    """
    reasons = []

    if not resolved.artist.value:
        reasons.append("needs_artist")

    if resolved.kind != "studio":
        date = resolved.date.value or {}
        year, month, day = date.get("year"), date.get("month"), date.get("day")
        if not year:
            reasons.append("needs_date")
        elif not month:
            reasons.append("needs_month")
        elif not day:
            reasons.append("needs_day")

    for name in _CONFLICT_TRACKED_FIELDS:
        f = getattr(resolved, name)
        if f.conflict:
            reasons.append(f"conflict:{name}")

    # Auto-ingest needs a CONFIDENT artist and, for a live recording, a CONFIDENT full date
    # (reader/confidence.py). A value a person set is locked: its Field is built confident.
    # Year-only and year-and-month dates already have their own reasons above.
    if resolved.artist.value and resolved.artist.confidence == "tentative":
        reasons.append("tentative:artist")
    if resolved.kind != "studio":
        date = resolved.date.value or {}
        if (date.get("year") and date.get("month") and date.get("day")
                and resolved.date.confidence == "tentative"):
            reasons.append("tentative:date")

    # Exact content duplicate short-circuits everything else: skipped, not
    # reviewed (spec section 9, resolved question 1).
    if any(d.kind == "content" for d in resolved.duplicates):
        return "skipped", ["duplicate_content"]

    if reasons:
        return "review", reasons
    return "ingested", reasons
