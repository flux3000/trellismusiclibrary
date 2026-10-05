"""
Evidence builders: turn database, resolver and Atlas state into EvidenceSections.
Skills pick from these; nothing here calls the network or the model.
"""
import json
import logging
import re

from sqlalchemy import func

from app.extensions import db
from app.lomax.prompts import EvidenceSection
from app.utils.format import format_partial_date

log = logging.getLogger("trellis.lomax")

# Info-file text is sent verbatim and is normally tiny, but a 28KB file exists. The cap is far
# above the real distribution, so in practice it never fires; it stops one pathological file
# from quietly tripling the cost of a run.
INFO_FILE_CHAR_CAP = 20_000
TRACK_CAP = 60
RESOLVER_FIELDS = ("artist", "date", "venue", "city", "state", "country", "event", "stage")
CURRENT_FIELDS = RESOLVER_FIELDS + ("source", "lineage")

_ISO_RE = re.compile(r"^\s*(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?\s*$")


def split_iso(text):
    """'1974', '1974-10' or '1974-10-19' -> (year, month, day) with None for missing parts;
    None for anything else (including an impossible month or day)."""
    m = _ISO_RE.match(str(text or ""))
    if not m:
        return None
    y, mo, d = int(m.group(1)), m.group(2), m.group(3)
    mo = int(mo) if mo else None
    d = int(d) if d else None
    if (mo is not None and not 1 <= mo <= 12) or (d is not None and not 1 <= d <= 31):
        return None
    if d is not None and mo is None:
        return None
    return y, mo, d


def date_text(value):
    """A resolver date value ({year, month, day}) or a string -> partial ISO text or ''."""
    if isinstance(value, dict):
        return format_partial_date(value.get("year"), value.get("month"), value.get("day")) or ""
    return str(value or "")


def stint_text(m):
    a = format_partial_date(m.start_year, m.start_month, m.start_day)
    b = format_partial_date(m.end_year, m.end_month, m.end_day)
    return "%s-%s" % (a or "", b or "") if (a or b) else ""


# ── what the page knows about a recording ────────────────────────────────────

def current_from_recording(rec):
    """The recording's values as the pages show them: location from the venue when there is one."""
    p = rec.performance
    v = p.venue if p else None
    return {
        "artist":  (p.artist.name if (p and p.artist) else ""),
        "date":    (format_partial_date(p.start_year, p.start_month, p.start_day) or "") if p else "",
        "venue":   (v.name if v else ""),
        "city":    (v.city if v else (p.city if p else "")) or "",
        "state":   (v.state if v else (p.state if p else "")) or "",
        "country": (v.country if v else (p.country if p else "")) or "",
        "event":   (p.event.name if (p and p.event) else ""),
        "stage":   (p.stage if p else "") or "",
        "source":  rec.source or "",
        "lineage": rec.lineage or "",
        "tracks":  [{"number": t.track_number, "title": t.title, "duration": t.duration,
                     "songwriter": t.songwriter or "", "notes": t.notes or ""} for t in rec.tracks],
        "info_file_content": rec.info_file_content or "",
    }


def known_fields_section(current, fields=CURRENT_FIELDS):
    lines = ["%s: %s" % (k, current[k]) for k in fields if current.get(k)]
    return EvidenceSection("known", ["The recording as filed:"] + lines) if lines else EvidenceSection("known")


# ── the library ──────────────────────────────────────────────────────────────

def roster_lines(artist, cap=40):
    out = []
    for m in artist.memberships[:cap]:
        bits = [b for b in (m.instrument, stint_text(m)) if b]
        out.append("%s%s" % (m.musician.name, " (%s)" % ", ".join(bits) if bits else ""))
    return out


def library_section(current, exclude_recording_id=None):
    """Artist row and roster, venue row, other recordings of this artist on this date."""
    from app.models.artist import Artist
    from app.models.performance import Performance
    from app.models.recording import Recording
    from app.models.venue import Venue

    lines = []
    name = (current.get("artist") or "").strip()
    artist = (db.session.query(Artist).filter(func.lower(Artist.name) == name.lower()).first()
              if name else None)
    if artist is not None:
        lines.append("Artist in the library: %s%s" % (
            artist.name, " (%s, %s)" % (artist.mb_type, artist.mb_area) if artist.mb_type and artist.mb_area
            else (" (%s)" % (artist.mb_type or artist.mb_area) if (artist.mb_type or artist.mb_area) else "")))
        if artist.memberships:
            lines.append("  roster: " + "; ".join(roster_lines(artist)))
    vname = (current.get("venue") or "").strip()
    venue = (db.session.query(Venue).filter(func.lower(Venue.name) == vname.lower()).first()
             if vname else None)
    if venue is not None:
        loc = ", ".join(x for x in (venue.city, venue.state, venue.country) if x)
        lines.append("Venue in the library: %s%s" % (venue.name, " (%s)" % loc if loc else ""))
    ymd = split_iso(current.get("date"))
    if artist is not None and ymd and ymd[0]:
        q = (db.session.query(Recording).join(Performance, Recording.performance_id == Performance.id)
             .filter(Performance.artist_id == artist.id, Performance.start_year == ymd[0]))
        if ymd[1]:
            q = q.filter(Performance.start_month == ymd[1])
        if ymd[2]:
            q = q.filter(Performance.start_day == ymd[2])
        if exclude_recording_id:
            q = q.filter(Recording.id != exclude_recording_id)
        for r in q.order_by(Recording.id).limit(8):
            pv = r.performance.venue
            lines.append("Other recording of this artist on this date: #%d%s%s" % (
                r.id, ", " + r.source if r.source else "", ", at " + pv.name if pv else ""))
    return EvidenceSection("known", lines, title="Known already (the archivist's database, "
                           "ground truth), library matches:") if lines else EvidenceSection("known")


def artist_row(name):
    from app.models.artist import Artist
    name = (name or "").strip()
    return (db.session.query(Artist).filter(func.lower(Artist.name) == name.lower()).first()
            if name else None)


# ── the Atlas ────────────────────────────────────────────────────────────────

def _atlas():
    try:
        from app.atlas.lookup import current_atlas
        return current_atlas()
    except Exception:  # noqa: BLE001 - an unreadable Atlas is not a reason to fail a run
        log.exception("lomax: Atlas unavailable")
        return None


def atlas_place_lines(atlas, cand):
    """One place candidate with its former names and years."""
    info = atlas.place_info(cand.id) or {}
    where = ", ".join(x for x in (info.get("city"), info.get("region"), info.get("country")) if x)
    out = ["Place: %s%s (matched %r, score %.2f)" % (cand.name, " in " + where if where else "",
                                                       cand.matched, cand.score)]
    names = [n for n in atlas.place_names(cand.id) if n[1] in ("official", "former")]
    if names:
        out.append("  names: " + "; ".join(
            "%s%s" % (n[0], " %s-%s" % (n[2] or "", n[3] or "") if (n[2] or n[3]) else "") for n in names[:8]))
    return out


def atlas_section(current):
    """Atlas matches for the artist, venue, event, and shows it knows for this act and date."""
    atlas = _atlas()
    if atlas is None:
        return EvidenceSection("reference")
    lines = []
    try:
        name = (current.get("artist") or "").strip()
        if name:
            for c in atlas.artist(name, limit=3):
                lines.append("Act: %s (matched %r, score %.2f)" % (c.name, c.matched, c.score))
        v = (current.get("venue") or "").strip()
        if v:
            for c in atlas.venue(v, limit=3):
                lines += atlas_place_lines(atlas, c)
        e = (current.get("event") or "").strip()
        if e:
            for c in atlas.event(e, limit=3):
                lines.append("Event: %s (score %.2f)" % (c.name, c.score))
        if name and current.get("date"):
            for s in atlas.event_place(name, current["date"])[:5]:
                lines.append("Atlas show on %s: %s at %s%s%s" % (
                    s.get("date"), s.get("act"), s.get("place") or "?",
                    ", " + s["city"] if s.get("city") else "", "" if s.get("exact") else " (partial date)"))
    except Exception:  # noqa: BLE001
        log.exception("lomax: Atlas lookup failed")
    return EvidenceSection("reference", lines)


# ── the resolver ─────────────────────────────────────────────────────────────

def resolver_scope(resolved, current, fields=RESOLVER_FIELDS):
    """Fields the resolver marked tentative or empty. Without a resolver reading, the fields
    that have no value on the page."""
    scope = []
    for name in fields:
        f = (resolved or {}).get(name)
        if isinstance(f, dict):
            if f.get("confidence") in ("tentative", "empty") or f.get("value") in (None, ""):
                scope.append(name)
        elif not current.get(name):
            scope.append(name)
    return scope


def resolver_section(resolved, scope):
    lines = []
    if not resolved:
        return EvidenceSection("files")
    if resolved.get("status"):
        lines.append("Resolver verdict: %s%s" % (resolved["status"], " (%s)" % ", ".join(resolved.get("reasons") or [])
                                                 if resolved.get("reasons") else ""))
    for name in RESOLVER_FIELDS:
        f = resolved.get(name)
        if not isinstance(f, dict):
            continue
        val = date_text(f.get("value")) if name == "date" else (f.get("value") or "")
        flags = [f.get("confidence") or "?"]
        if f.get("source"):
            flags.append("from " + f["source"])
        if f.get("conflict"):
            flags.append("sources disagree")
        lines.append("%s: %s [%s]%s" % (name, val or "(empty)", "; ".join(flags),
                                        " IN SCOPE" if name in scope else ""))
        for ev in (f.get("evidence") or [])[:3]:
            if ev.get("text"):
                lines.append("    %s%s: %s" % (ev.get("source") or "?",
                                               " line %d" % (ev["line"] + 1) if ev.get("line") is not None else "",
                                               ev["text"]))
        ru = f.get("runner_up")
        if ru and ru.get("value"):
            lines.append("    runner-up: %s (from %s)" % (date_text(ru["value"]) if name == "date" else ru["value"],
                                                       ru.get("source") or "?"))
    return EvidenceSection("files", lines, title="Read from the files (the resolver's readings, one source, "
                           "never corroboration):")


# ── tracks and info text ─────────────────────────────────────────────────────

def tracks_section(current):
    tracks = current.get("tracks") or []
    if not tracks:
        return EvidenceSection("files")
    lines = ["Tracks on disk (%d), titles may be missing or uncertain:" % len(tracks)]
    for t in tracks[:TRACK_CAP]:
        dur = t.get("duration")
        lines.append("  %s. %s%s%s%s" % (
            t.get("number", "?"), t.get("title") or "(untitled)",
            " [%d:%02d]" % (int(dur) // 60, int(dur) % 60) if dur else "",
            " | songwriter: " + t["songwriter"] if t.get("songwriter") else "",
            " | note: " + t["notes"] if t.get("notes") else ""))
    return EvidenceSection("files", lines)


def info_file_section(current):
    text = (current.get("info_file_content") or "").strip()
    if not text:
        return EvidenceSection("files")
    cut = len(text) > INFO_FILE_CHAR_CAP
    if cut:
        text = text[:INFO_FILE_CHAR_CAP]
    lines = ["Info file contents, as found in the recording's folder (it may contain an unnumbered "
             "setlist, see the parsing rules):", "---"] + text.split("\n") + (
        ["[TRUNCATED: this info file was too long to send in full. If the setlist appears to be cut "
         "off, say so in verify_items rather than guessing the rest.]"] if cut else []) + ["---"]
    return EvidenceSection("files", ["  " + l if l else "  " for l in lines])


# ── artist and venue ─────────────────────────────────────────────────────────

def artist_known_section(artist):
    from app.models.performance import Performance
    from app.models.recording import Recording
    lines = []
    for label, val in (("Type", artist.mb_type), ("Origin", artist.mb_area),
                       ("Active from", artist.mb_begin), ("Active until", artist.mb_end),
                       ("Disambiguation", artist.mb_disambiguation),
                       ("Genre", artist.genre.name if artist.genre else None)):
        if val:
            lines.append("%s: %s" % (label, val))
    if artist.memberships:
        lines.append("Roster on the record (%d stints): %s" % (len(artist.memberships),
                                                              "; ".join(roster_lines(artist))))
    if artist.resources:
        lines.append("Resource links already on the record: " + "; ".join(
            "%s <%s>" % (r.label or r.url, r.url) for r in artist.resources))
    n, lo, hi = (db.session.query(func.count(Recording.id), func.min(Performance.start_year),
                                  func.max(Performance.start_year))
                 .join(Performance, Recording.performance_id == Performance.id)
                 .filter(Performance.artist_id == artist.id).one())
    if n:
        lines.append("Recordings in the library: %d%s" % (n, ", dated %s to %s" % (lo, hi) if lo else ""))
    return EvidenceSection("known", lines)


def venue_known_section(venue):
    from app.models.performance import Performance
    from app.models.recording import Recording
    lines = ["Name: " + venue.name]
    for label, val in (("City", venue.city), ("State", venue.state), ("Country", venue.country)):
        if val:
            lines.append("%s: %s" % (label, val))
    rows = (db.session.query(Performance.start_year, Performance.start_month, Performance.start_day,
                             Performance.artist_id)
            .join(Recording, Recording.performance_id == Performance.id)
            .filter(Performance.venue_id == venue.id).all())
    if rows:
        dated = sorted(format_partial_date(*r[:3]) for r in rows if r[0])
        lines.append("Recordings in the library at this venue: %d%s" % (
            len(rows), ", first %s, last %s" % (dated[0], dated[-1]) if dated else ""))
        from app.models.artist import Artist
        ids = {r[3] for r in rows}
        names = [n for (n,) in db.session.query(Artist.name).filter(Artist.id.in_(ids)).order_by(Artist.name).limit(15)]
        lines.append("Acts recorded there: " + ", ".join(names))
    return EvidenceSection("known", lines)


def venue_atlas_section(venue):
    atlas = _atlas()
    if atlas is None:
        return EvidenceSection("reference")
    lines = []
    try:
        for c in atlas.venue(venue.name, limit=3):
            lines += atlas_place_lines(atlas, c)
    except Exception:  # noqa: BLE001
        log.exception("lomax: Atlas lookup failed")
    return EvidenceSection("reference", lines)
