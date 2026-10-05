"""Recording: one job for a recording's details, track titles, songwriters and track notes."""
import json

from app.lomax import evidence as ev
from app.lomax.prompts import EvidenceSection
from app.lomax.skills.base import Skill
from app.lomax.sources import trusted_section

FIELDS = list(ev.CURRENT_FIELDS)          # the ten Resolver fields
TRACK_PIECES = (("title", "title"), ("songwriter", "songwriter"), ("note", "notes"))

INSTRUCTIONS = """SKILL: Recording.
One job for this recording: its details, then its track listing with songwriters and notes.
- Return a value for EVERY field (artist, date, venue, city, state, country, event, stage, \
source, lineage) in 'proposals', including values you agree with: set agrees=true on those and \
leave 'proposed' equal to the filed value. A field you found nothing on may be left out.
- "Fields in scope" are the ones the resolver marked tentative or empty: spend your effort \
there. For any other field the resolver was confident, so propose a different value only with \
evidence that ties it to THIS recording (for example, research that establishes a corrected \
date). The rule about not replacing a specific value still applies in full. Say in 'thinking' \
what that evidence is.
- "Read from the files" readings are one source. Corroborate them against the Atlas, the \
library and the trusted sources, not against themselves.
- Tracks: return an entry in 'tracks' for EVERY track on disk, as {number, title, songwriter, \
note}, ordered to match the audio files. Give your best title for each (the filed one when you \
agree), the songwriter when you can establish one, and a short note only when something is \
worth recording (a segue, a guest, a cut). Build the titles from the info file text and, when \
your level allows searching, from the trusted sources first (setlist.fm, etreedb.org, the Live \
Music Archive) and then the open web. If the audio file count does not match the setlist you \
find, still return your best-ordered titles and flag the count discrepancy in verify_items. If \
you cannot find a setlist anywhere, say so in verify_items ("No setlist found for this \
recording"). Never invent titles or songwriters.
- The info file often contains a real setlist typed as plain, unnumbered lines, with no track \
numbers, in the spot where a tracklist normally goes. Treat that as a primary source:
  * Segue notation appended to a title ("->", "-->", "/") marks a transition into the next \
song. Strip it; it is not part of the title.
  * Footnote markers appended to a title (*, **, ***, a dagger, ^, or a bracketed number) \
point to an annotation elsewhere in the file, often personnel notes. Strip the marker from \
the title; the annotation is worth keeping but belongs in provenance_notes.
  * Section labels ("Set I", "Set II", "Encore", "Disc 1", "Intro") are structural \
headers, not songs. Skip them.
  * A line that is only a time value (such as 3:40) is a duration, not a title. Drop it.
  * Named improvisation or instrumental segments ("Drums", "Bass", "Jam", "Tuning") ARE \
legitimate track titles in a live setlist. Keep them.
  * Cross-check the candidate list against your research to raise confidence and correct \
spelling and capitalization. A count mismatch alone is not a reason to withhold titles; \
flag it in verify_items.
- verify_items are things the archivist should check by ear (transient). provenance_notes \
are lasting facts worth keeping in the info file (date disputes, broadcast or remaster \
context, source anomalies, lineup annotations)."""

_CONF = {"type": "string", "enum": ["high", "medium", "low"]}
SUBMIT = {
    "name": "submit_recording_research",
    "description": "Submit the recording's details, tracks and notes for human review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "thinking": {"type": "string"},
            "answer": {"type": "string", "description": "Direct answer to the archivist's question, if one was asked."},
            "proposals": {"type": "array", "items": {"type": "object", "properties": {
                "field": {"type": "string", "enum": FIELDS},
                "current": {"type": "string"},
                "proposed": {"type": "string"},
                "agrees": {"type": "boolean", "description": "True when this equals the filed value."},
                "confidence": _CONF,
                "source": {"type": "string", "enum": ["web", "trusted", "atlas", "db_match", "info_file", "tags"]},
                "url": {"type": "string"}},
                "required": ["field", "proposed", "confidence", "source"]}},
            "tracks": {"type": "array", "description": "One entry per track on disk, ordered to match the files.",
                       "items": {"type": "object", "properties": {
                           "number": {"type": "integer"}, "title": {"type": "string"},
                           "songwriter": {"type": "string"}, "note": {"type": "string"}},
                           "required": ["number", "title"]}},
            "verify_items": {"type": "array", "items": {"type": "string"}},
            "provenance_notes": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "url": {"type": "string"}}}},
        },
        "required": ["thinking", "proposals"],
    },
}


def _resolved(subject):
    resolved = subject.current.get("resolved")
    if resolved is None and subject.type == "recording":
        try:
            resolved = json.loads(subject.obj.resolver_json) if subject.obj.resolver_json else None
        except (ValueError, TypeError):
            resolved = None
    return resolved


def gather(subject, ctx):
    cur = subject.current
    resolved = _resolved(subject)
    scope = ev.resolver_scope(resolved, cur)
    subject.scratch["scope"] = scope
    rec_id = subject.id if subject.type == "recording" else None
    artist = ev.artist_row(cur.get("artist"))
    return [
        EvidenceSection("task", ["Fields in scope (tentative or empty): " + ", ".join(scope)] if scope else
                        ["The resolver settled every field. Look only for known corrections, each "
                         "backed by evidence tying the different value to this recording."]),
        ev.known_fields_section(cur),
        ev.library_section(cur, exclude_recording_id=rec_id),
        ev.atlas_section(cur),
        ev.resolver_section(resolved, scope),
        ev.tracks_section(cur),
        ev.info_file_section(cur),
        trusted_section(artist),
    ]


def _same(a, b):
    return " ".join(str(a or "").lower().split()) == " ".join(str(b or "").lower().split())


def normalize(raw, subject):
    scope = set(subject.scratch.get("scope") or [])
    cur = subject.current
    props = []
    for p in raw.get("proposals") or []:
        if not (isinstance(p, dict) and p.get("field") in FIELDS and p.get("proposed")):
            continue
        agrees = bool(p.get("agrees")) or _same(p["proposed"], cur.get(p["field"]))
        props.append(dict(p, agrees=agrees, challenge=(not agrees) and p["field"] in ev.RESOLVER_FIELDS and p["field"] not in scope))
    filed = {t.get("number"): t for t in cur.get("tracks") or []}
    tracks = []
    for t in raw.get("tracks") or []:
        if not (isinstance(t, dict) and isinstance(t.get("number"), int)):
            continue
        tracks.append({k: t.get(k) or "" for k in ("number", "title", "songwriter", "note")})
        now = filed.get(t["number"]) or {}
        for key, filed_key in TRACK_PIECES:
            val = (t.get(key) or "").strip()
            if not val:
                continue
            was = now.get(filed_key)
            props.append({"field": "track.%d.%s" % (t["number"], key), "current": was or "", "proposed": val,
                          "confidence": "medium", "source": "web", "agrees": _same(val, was)})
    result = {"thinking": raw.get("thinking", ""), "answer": raw.get("answer", ""),
              "proposals": props, "tracks": tracks,
              "track_titles": [{"number": t["number"], "title": t["title"]} for t in tracks if t["title"]],
              "verify_items": raw.get("verify_items") or [],
              "provenance_notes": raw.get("provenance_notes") or [],
              "sources": raw.get("sources") or [], "scope": sorted(scope)}
    return result, props


SKILL = Skill(key="recording", subject_types=("recording", "folder"), instructions=INSTRUCTIONS,
              submit_tool=SUBMIT, gather=gather, normalize=normalize, base_tokens=8000, label="Recording")
