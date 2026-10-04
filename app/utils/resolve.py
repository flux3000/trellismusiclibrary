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
    candidates   {source: value} for every source that offered a value, so a
                 UI can show "tags say X, info file says Y" without
                 re-deriving anything
    conflict     True when two sources both state something for this field
                 and disagree
    """
    value: object = None
    source: str = None
    candidates: dict = _dc_field(default_factory=dict)
    conflict: bool = False

    def to_dict(self):
        return {"value": self.value, "source": self.source,
                "candidates": dict(self.candidates), "conflict": self.conflict}


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
        }


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
    m = FOLDER_DATE_RE.search(folder_name or "")
    if not m:
        return None
    g = m.group(0)
    return (int(g[:4]), int(g[5:7]), int(g[8:10]))


def _date_precision(v):
    return sum(1 for c in v if c is not None)


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

    return Field(
        value=_as_dict(value),
        source=best_source,
        candidates={s: _as_dict(v) for s, v in cands.items()},
        conflict=conflict,
    )


# ── Artist ────────────────────────────────────────────────────────────────

def _act_key_norm(name):
    """Same normalisation resolve_similar_artist_ids() uses -- imported
    lazily to avoid a hard dependency on the DB-touching artists module at
    import time (this file must stay importable with no app context)."""
    from app.utils.artists import _act_key
    return _act_key(name)


def _resolve_artist_field(scan, *, library_root=None, placement=None):
    from_tags = scan["suggestions"]["from_tags"]
    from_info = scan["suggestions"]["from_info_file"]

    cands = {}
    tag_artist = _consistent_tag(from_tags.get("tracks", []), "artist", "albumartist")
    if tag_artist:
        cands["tags"] = tag_artist
    if from_info.get("artist"):
        cands["info"] = from_info["artist"]
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

    source = "tags" if "tags" in cands else ("info" if "info" in cands else "folder")
    cased = {k: title_case(v) for k, v in cands.items()}
    return Field(value=cased[source], source=source, candidates=cased, conflict=conflict)


# ── Venue + city/state/country ───────────────────────────────────────────

def _resolve_venue_field(scan):
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
    return Field(value=cands[source], source=source, candidates=dict(cands), conflict=False)


def _resolve_info_only_field(scan, key):
    """event / stage: what the info file's header states. Tags carry neither, and the
    folder name is not read for them, so there is one source and no conflict to track.
    The festival is an Event, "Harbor Stage" is the performance's Stage; neither is
    ever the venue (the reader already keeps them apart)."""
    v = scan["suggestions"]["from_info_file"].get(key)
    v = v.strip() if isinstance(v, str) else v
    if key == "event":
        from app.utils.event_names import clean_event_name
        v = clean_event_name(v)
    if not v:
        return Field()
    return Field(value=v, source="info", candidates={"info": v}, conflict=False)


def _resolve_location_fields(scan, venue_field):
    """city/state/country: taken from the SAME source as the venue where
    that source has them; filled from the other only when it has none."""
    from_tags = scan["suggestions"]["from_tags"]
    from_info = scan["suggestions"]["from_info_file"]
    sources = {"tags": from_tags, "info": from_info}
    primary = venue_field.source

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
        out[key] = Field(value=cands[source], source=source, candidates=dict(cands), conflict=False)
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


# ── resolve() ─────────────────────────────────────────────────────────────

def resolve(scan, *, library_root=None, placement=None):
    """
    scan is build_scan_payload()'s output. Returns a Resolved with every
    field, the track list, kind, and an empty duplicates list (call
    find_duplicates() separately -- it is the one part of this module that
    touches the DB).
    """
    date_f = _resolve_date_field(scan)
    artist_f = _resolve_artist_field(scan, library_root=library_root, placement=placement)
    venue_f = _resolve_venue_field(scan)
    loc = _resolve_location_fields(scan, venue_f)
    album_f = _resolve_album_field(scan)
    kind = classify_kind(scan, album_f.value, venue_f.value)
    tracks = resolve_tracks(scan)

    return Resolved(
        date=date_f,
        artist=artist_f,
        venue=venue_f,
        event=_resolve_info_only_field(scan, "event"),
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

    # Exact content duplicate short-circuits everything else: skipped, not
    # reviewed (spec section 9, resolved question 1).
    if any(d.kind == "content" for d in resolved.duplicates):
        return "skipped", ["duplicate_content"]

    if reasons:
        return "review", reasons
    return "ingested", reasons
