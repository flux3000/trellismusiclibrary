"""Album: one job for a studio record. Pulls details and stories about the album: its title and
year, the track titles and songwriters, and the album story (notes). Live-show matters (venue,
taper, lineage, setlists) are not in this skill."""
import re
from datetime import date

from app.lomax import evidence as ev
from app.lomax import filters
from app.lomax.prompts import EvidenceSection
from app.lomax.skills.base import Skill
from app.lomax.sources import artist_links

FIELDS = ("title", "year", "notes")
TRACK_PIECES = (("title", "title"), ("songwriter", "songwriter"))
NOTES_MAX_CHARS = 4000

INSTRUCTIONS = """SKILL: Album.
This skill is for a studio record (an album), not a live show. Ignore the live-recording \
guidance in the shared rules above where it does not apply: there is no venue, date, taper, \
lineage or setlist here. The filed artist and title name the album. One job: pull details and \
stories about the album.
- Return proposals ONLY for these fields: 'title' (the album title as released), 'year' (the \
four-digit year of first release) and 'notes' (the album story). Set agrees=true on a title or \
year you agree with and leave 'proposed' equal to the filed value. A field you found nothing \
on may be left out. Never propose any other field.
- 'notes' is a few short paragraphs of plain prose, separated by blank lines, about the album: \
the making of it (where and when it was recorded, the producer and label, the sessions), the \
personnel on the record, how it was received, and any well-documented stories connected with \
it. Keep every statement factual and traceable to a source you found; leave out anything you \
cannot source. No marketing tone, no superlatives, no praise words, no em dashes, no \
markdown, no HTML and no citation markers. Put the sources in 'sources'. When notes are \
already filed, build on them without repeating them, and propose a replacement text only when \
yours is better sourced or fuller.
- Tracks: return an entry in 'tracks' for each track listed, as {number, title, songwriter}, \
ordered as listed. Give the title as released (the filed one when you agree) and the \
songwriter or songwriters when you can establish them from the release credits. Never invent \
titles or songwriters. If the number of tracks differs from the released track list, still \
return your best match and flag the difference in verify_items. Do not write track notes.
- Weigh the evidence in this order: the archivist's filed values, then MusicBrainz release \
facts when listed, then the info file when one is present, then trusted sources (MusicBrainz, \
Discogs, the label, the artist's own pages), then the open web. An info file, when present, \
is one source; corroborate it elsewhere.
- verify_items are things the archivist should check (for example a title that differs between \
editions or countries)."""

_CONF = {"type": "string", "enum": ["high", "medium", "low"]}
SUBMIT = {
    "name": "submit_album_research",
    "description": "Submit the album's title, year, track details and album story for human review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "thinking": {"type": "string"},
            "answer": {"type": "string", "description": "Direct answer to the archivist's question, if one was asked."},
            "proposals": {"type": "array", "items": {"type": "object", "properties": {
                "field": {"type": "string", "enum": list(FIELDS)},
                "current": {"type": "string"},
                "proposed": {"type": "string", "description": "For notes: a few short paragraphs of plain, sourced prose."},
                "agrees": {"type": "boolean", "description": "True when this equals the filed value."},
                "confidence": _CONF,
                "source": {"type": "string", "enum": ["web", "trusted", "atlas", "db_match", "info_file", "tags"]},
                "url": {"type": "string"}},
                "required": ["field", "proposed", "confidence", "source"]}},
            "tracks": {"type": "array", "description": "One entry per listed track, in order.",
                       "items": {"type": "object", "properties": {
                           "number": {"type": "integer"}, "title": {"type": "string"},
                           "songwriter": {"type": "string"}},
                           "required": ["number", "title"]}},
            "verify_items": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "url": {"type": "string"}}}},
        },
        "required": ["thinking", "proposals"],
    },
}


def current_from_album(rec):
    """A studio recording as the Add Recording page would send it for a folder run."""
    p = rec.performance
    return {
        "artist": (p.artist.name if (p and p.artist) else ""),
        "title": rec.title or "",
        "year": str(p.start_year) if (p and p.start_year) else "",
        "tracks": [{"number": t.track_number, "title": t.title, "duration": t.duration,
                    "songwriter": t.songwriter or ""} for t in rec.tracks],
        "info_file_content": rec.info_file_content or "",
        "notes": rec.notes or "",
        "mb_label": rec.mb_label or "", "mb_catalog_number": rec.mb_catalog_number or "",
        "mb_release_country": rec.mb_release_country or "", "mb_release_id": rec.mb_release_id or "",
    }


def _current(subject):
    cur = dict(subject.current or {})
    if subject.type == "recording" and subject.obj is not None:
        cur = current_from_album(subject.obj)
    for k in ("artist", "title", "year", "notes"):
        cur[k] = str(cur.get(k) or "").strip()
    return cur


def _known_section(cur):
    lines = ["The album as filed:"]
    for label, key in (("artist", "artist"), ("title", "title"), ("year", "year")):
        if cur.get(key):
            lines.append("  %s: %s" % (label, cur[key]))
    mb = [("label", "mb_label"), ("catalog number", "mb_catalog_number"),
          ("release country", "mb_release_country"), ("MusicBrainz release id", "mb_release_id")]
    mb_lines = ["  %s: %s" % (l, cur[k]) for l, k in mb if cur.get(k)]
    if mb_lines:
        lines.append("MusicBrainz release facts already on the record:")
        lines += mb_lines
    if cur.get("notes"):
        lines.append("Notes already filed on this album (the archivist's own text):")
        lines += ["  " + l if l else "  " for l in cur["notes"].split("\n")]
    return EvidenceSection("known", lines) if len(lines) > 1 else EvidenceSection("known")


def _tags_section(cur):
    tags = cur.get("tags")
    if isinstance(tags, dict):
        lines = ["%s: %s" % (k, v) for k, v in tags.items() if v]
    elif isinstance(tags, (list, tuple)):
        lines = [str(t) for t in tags if t]
    else:
        lines = []
    return EvidenceSection("files", ["Tags read from the audio files:"] + lines) if lines else EvidenceSection("files")


def _trusted_section(artist):
    lines = []
    links = artist_links(artist)
    if links:
        lines.append("For this act:")
        lines += ["  %s: %s (%s)" % (l["label"], l["url"], l["origin"]) for l in links]
    lines.append("General sources for a studio record:" if links else "Sources for a studio record:")
    lines += ["  MusicBrainz: https://musicbrainz.org (release credits, track lists, labels)",
              "  Discogs: https://www.discogs.com (editions, credits, catalog numbers)",
              "  Wikipedia: https://en.wikipedia.org (album articles with sourced background)"]
    return EvidenceSection("trusted", lines)


def gather(subject, ctx):
    cur = _current(subject)
    subject.scratch["question"] = ctx.question
    subject.scratch["current"] = cur
    artist = ev.artist_row(cur.get("artist"))
    task = ["Research this studio album: confirm its title and year, establish the track titles "
            "and songwriters, and write the album story for the notes."]
    return [
        EvidenceSection("task", task),
        _known_section(cur),
        ev.tracks_section(cur),
        _tags_section(cur),
        ev.info_file_section(cur),
        _trusted_section(artist),
    ]


_same = filters.same_text
_YEAR = re.compile(r"\b(1[89]\d\d|2\d\d\d)\b")
_MD = re.compile(r"(\*\*|__|`|^#{1,6}\s*)", re.M)


def _year(text):
    m = _YEAR.search(str(text or ""))
    if not m:
        return ""
    y = int(m.group(1))
    return m.group(1) if 1850 <= y <= date.today().year + 1 else ""


def clean_notes(text, asked=False):
    """The album story as stored: citation markup, markdown and em dashes removed, sentences about
    a question nobody asked dropped, paragraphs kept, length capped at a paragraph boundary."""
    from app.lomax.core import clean_prose
    t = clean_prose(str(text or ""))
    t = _MD.sub("", t)
    t = re.sub(r"\s*—\s*", ", ", t)
    t = re.sub(r"<[^>]+>", "", t)
    if not asked:
        t = filters.drop_absent_question_sentences(t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) > NOTES_MAX_CHARS:
        cut = t[:NOTES_MAX_CHARS]
        end = max(cut.rfind("\n\n"), cut.rfind(". "))
        t = cut[:end + 1].strip() if end > 0 else cut.strip()
    return t


def normalize(raw, subject):
    cur = subject.scratch.get("current") or _current(subject)
    asked = bool((subject.scratch.get("question") or "").strip())
    props, seen = [], set()
    for p in raw.get("proposals") or []:
        if not (isinstance(p, dict) and p.get("field") in FIELDS and p.get("proposed")) or p["field"] in seen:
            continue
        f = p["field"]
        if f == "year":
            val = _year(p["proposed"])
        elif f == "notes":
            val = clean_notes(p["proposed"], asked)
        else:
            val = re.sub(r"\s*—\s*", ", ", " ".join(str(p["proposed"]).split()))
        if not val:
            continue
        seen.add(f)
        agrees = bool(p.get("agrees")) or _same(val, cur.get(f))
        props.append(dict(p, proposed=val, current=cur.get(f) or p.get("current") or "", agrees=agrees,
                          challenge=False))
    filed = {t.get("number"): t for t in cur.get("tracks") or []}
    tracks = []
    for t in raw.get("tracks") or []:
        if not (isinstance(t, dict) and isinstance(t.get("number"), int)):
            continue
        now = filed.get(t["number"]) or {}
        tracks.append({"number": t["number"], "title": t.get("title") or "", "songwriter": t.get("songwriter") or ""})
        for key, filed_key in TRACK_PIECES:
            val = str(t.get(key) or "").strip()
            if not val:
                continue
            was = now.get(filed_key)
            props.append({"field": "track.%d.%s" % (t["number"], key), "current": was or "", "proposed": val,
                          "confidence": "medium", "source": "web", "agrees": _same(val, was)})
    answer = raw.get("answer", "") if asked else ""
    thinking = raw.get("thinking", "")
    if not asked:
        thinking = filters.drop_absent_question_sentences(thinking)
    result = {"skill": "album", "thinking": thinking, "answer": answer, "proposals": props, "tracks": tracks,
              "track_titles": [{"number": t["number"], "title": t["title"]} for t in tracks if t["title"]],
              "verify_items": raw.get("verify_items") or [], "sources": raw.get("sources") or []}
    return result, props


SKILL = Skill(key="album", subject_types=("recording", "folder"), instructions=INSTRUCTIONS,
              submit_tool=SUBMIT, gather=gather, normalize=normalize, base_tokens=6000, label="Album")
