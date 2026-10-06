"""Recording: one job for a recording's details, track titles, songwriters and track notes."""
import json
import re

from app.lomax import evidence as ev
from app.lomax import filters
from app.lomax.prompts import EvidenceSection
from app.lomax.skills.base import Skill
from app.lomax.sources import trusted_section

FIELDS = list(ev.CURRENT_FIELDS)          # the ten Resolver fields
TRACK_PIECES = (("title", "title"), ("songwriter", "songwriter"), ("note", "notes"))

INSTRUCTIONS = """SKILL: Recording.
One job for this recording: its details, then its track listing with songwriters and notes.
- Return a value for EVERY field (artist, date, venue, city, state, country, event, stage, \
source) in 'proposals', including values you agree with: set agrees=true on those and \
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
agree) and the songwriter when you can establish one. A track 'note' exists ONLY to carry what the info file says about that track, through a \
footnote mark beside the track that a footer line explains ("* with Jerry Garcia on pedal \
steel"), or a parenthetical or bracketed annotation on the track's own line. Copy that text, \
short, in the info file's own words. NEVER write a note that: describes what the track is \
(tuning, intro, band intro, banter, applause, crowd, announcer, encore break, segue, opening \
improvisation, not a performed piece); gives album, release, year, writer history or cover-of \
facts; or holds anything not written next to that track or in its footnote, including anything \
you know or found elsewhere. No mark and no annotation in the info file for a track means an \
empty 'note'. No exceptions for helpful context. Build the titles from the info file text and, when \
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
the title; a short per-track annotation becomes that track's note, and anything about the \
recording as a whole belongs in provenance_notes.
  * Section labels ("Set I", "Set II", "Encore", "Disc 1", "Intro") are structural \
headers, not songs. Skip them.
  * A line that is only a time value (such as 3:40) is a duration, not a title. Drop it.
  * Named improvisation or instrumental segments ("Drums", "Bass", "Jam", "Tuning") ARE \
legitimate track titles in a live setlist. Keep them.
  * Cross-check the candidate list against your research to raise confidence and correct \
spelling and capitalization. A count mismatch alone is not a reason to withhold titles; \
flag it in verify_items.
- Lineage goes in the 'lineage' list, never in 'proposals'. Lineage is the technical path the \
recording took, from the microphones or source through the recorder, transfer and editing to \
the file format, and nothing else. Give it as short steps, each a noun phrase of at most about \
60 characters: "AUD", "Sony ECM-909 > Sony TC-D5M", "DAT", "CDR", "EAC", "FLAC 16/44.1". A whole \
lineage may be as simple as "AUD > DAT". A step is never a sentence and never holds reporting \
words (per, said, refers, notes), dates, place names or people; the one exception is a taper \
credit, "Taper: Name", as its own step when the info file states one. Leave 'lineage' empty \
when the files give no chain. Any discrepancy or explanation about lineage goes in \
verify_items or thinking, never in lineage.
- Genre: when the act this recording will be filed under has no genre, propose ONE genre. That \
act is the filed artist, or the artist you propose when you propose a different one (judge the \
genre for that artist, the filed name may be a misread). The genre comes from general \
knowledge and needs no search: if the act is well known, propose its primary genre. Choose \
from the Trellis genres listed when one fits, and a short plain name (at most four words) only \
when none does. Never propose a genre for an act that already has one.
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
                "field": {"type": "string", "enum": [f for f in FIELDS if f != "lineage"] + ["genre"]},
                "current": {"type": "string"},
                "proposed": {"type": "string"},
                "agrees": {"type": "boolean", "description": "True when this equals the filed value."},
                "confidence": _CONF,
                "source": {"type": "string", "enum": ["web", "trusted", "atlas", "db_match", "info_file", "tags"]},
                "url": {"type": "string"}},
                "required": ["field", "proposed", "confidence", "source"]}},
            "lineage": {"type": "array", "items": {"type": "string"},
                        "description": "The recording chain as short steps, each at most about 60 characters, from source or microphones through recorder, transfer and editing to file format, e.g. [\"AUD\", \"DAT\", \"FLAC 16/44.1\"]. Never sentences, reporting words, dates, places or people (except a step \"Taper: Name\"). Empty when unknown."},
            "tracks": {"type": "array", "description": "One entry per track on disk, ordered to match the files.",
                       "items": {"type": "object", "properties": {
                           "number": {"type": "integer"}, "title": {"type": "string"},
                           "songwriter": {"type": "string"},
                           "note": {"type": "string", "description": "Only text the info file writes next to this track (a footnote its mark points to, or a parenthetical annotation). Never what the track is, never album or year, never outside knowledge. Empty otherwise."}},
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
    subject.scratch["question"] = ctx.question
    rec_id = subject.id if subject.type == "recording" else None
    artist = ev.artist_row(cur.get("artist"))
    filed_genre = ev.act_genre_name(cur)
    genre_open = not filed_genre
    subject.scratch["genre_open"] = genre_open
    task = (["Fields in scope (tentative or empty): " + ", ".join(scope)] if scope else
            ["The resolver settled every field. Look only for known corrections, each "
             "backed by evidence tying the different value to this recording."])
    genres = []
    if genre_open:
        task.append("Genre is open: the filed act has no genre. Propose its primary genre (general "
                    "knowledge, no search needed); for a different artist you propose, judge that artist.")
    else:
        task.append("The filed act already has the genre %s. Propose a genre only if you propose a "
                    "different artist who has none." % filed_genre)
    names = ev.genre_names()
    if names:
        genres = ["  " + "; ".join(names)]
    return [
        EvidenceSection("task", task),
        ev.known_fields_section(cur),
        EvidenceSection("known", genres, title="Trellis genres (choose one of these when one fits):"),
        ev.library_section(cur, exclude_recording_id=rec_id),
        ev.atlas_section(cur),
        ev.resolver_section(resolved, scope),
        ev.tracks_section(cur),
        ev.info_file_section(cur),
        trusted_section(artist),
    ]


_same = filters.same_text


def _one_genre(text):
    """One genre per act. The whole string is tried against the Trellis genres first ("R&B",
    "Rhythm and Blues"); only then is a list split, on commas, semicolons and slashes, keeping the
    first part. An existing genre keeps its own spelling; a new name must be a plain short name."""
    from app.utils import musicbrainz as mb
    whole = " ".join(str(text or "").split())
    if not whole:
        return ""
    found = ev.canonical_genre(whole)
    if found:
        return found
    first = re.split(r"\s*[,;/]\s*", whole, maxsplit=1)[0].strip()
    found = ev.canonical_genre(first)
    if found:
        return found
    return first[:80] if mb.valid_new_genre(first) else ""


def _field_proposal(p, cur, scope, genre_open, country=""):
    """One field proposal after Lomax's own rules are enforced, or None to drop it."""
    f, val = p["field"], str(p["proposed"]).strip()
    if f == "genre":
        if not genre_open:
            return None
        val = _one_genre(val)
        return dict(p, proposed=val, current="", agrees=False, challenge=False) if val else None
    if f == "state":
        val = filters.state_code(val, country)
        was = filters.state_code(cur.get(f), country)
        agrees = _same(val, was)
        return dict(p, proposed=val, current=cur.get(f) or p.get("current"), agrees=agrees,
                    challenge=(not agrees) and f not in scope)   # state is a resolver field
    if f == "lineage":
        val = filters.clean_lineage(val)
        if not val:
            return None
        agrees = _same(val, cur.get(f))     # the flag is ignored: a mixed filed value is never agreed with
        return dict(p, proposed=val, agrees=agrees, challenge=False)      # lineage is not a resolver field
    agrees = bool(p.get("agrees")) or _same(val, cur.get(f))
    return dict(p, proposed=val, agrees=agrees,
                challenge=(not agrees) and f in ev.RESOLVER_FIELDS and f not in scope)


def normalize(raw, subject):
    scope = set(subject.scratch.get("scope") or [])
    genre_open = bool(subject.scratch.get("genre_open"))
    # The genre is judged for the act the recording will be filed under: a different artist Lomax
    # proposes (the filed name may be a misread) decides it, whatever the filed act has.
    filed_artist = str(subject.current.get("artist") or "").strip().lower()
    new_artist = next((str(p.get("proposed") or "").strip() for p in raw.get("proposals") or []
                       if isinstance(p, dict) and p.get("field") == "artist" and not p.get("agrees")
                       and str(p.get("proposed") or "").strip().lower() not in ("", filed_artist)), "")
    if new_artist:
        row = ev.artist_row(new_artist)
        genre_open = not (row is not None and row.genre)
    asked = bool((subject.scratch.get("question") or "").strip())
    cur = subject.current
    info_text = cur.get("info_file_content") or ""
    props = []
    # The country decides which state list applies: the filed one, else the one Lomax proposes.
    country = cur.get("country") or next((str(p.get("proposed") or "") for p in raw.get("proposals") or []
                                          if isinstance(p, dict) and p.get("field") == "country"), "")
    for p in raw.get("proposals") or []:
        if not (isinstance(p, dict) and (p.get("field") in FIELDS or p.get("field") == "genre") and p.get("proposed")):
            continue
        q = _field_proposal(p, cur, scope, genre_open, country)
        if q is not None:
            props.append(q)
    # Lineage arrives as a list of chain steps; a lineage proposal in the old string form is cleaned the
    # same way. One lineage proposal at most, and the list wins.
    steps = raw.get("lineage")
    if isinstance(steps, (list, tuple)) and steps:
        chain = filters.clean_lineage(steps)
        props = [q for q in props if q["field"] != "lineage"]
        if chain:
            agrees = _same(chain, cur.get("lineage"))
            props.append({"field": "lineage", "current": cur.get("lineage") or "", "proposed": chain,
                          "confidence": "medium", "source": "info_file", "agrees": agrees, "challenge": False})
    filed = {t.get("number"): t for t in cur.get("tracks") or []}
    tracks = []
    for t in raw.get("tracks") or []:
        if not (isinstance(t, dict) and isinstance(t.get("number"), int)):
            continue
        now = filed.get(t["number"]) or {}
        title = (t.get("title") or "").strip()
        note = filters.filter_track_note(t.get("note"), title or now.get("title"), info_text, now.get("notes"), t["number"])
        tracks.append({"number": t["number"], "title": t.get("title") or "",
                       "songwriter": t.get("songwriter") or "", "note": note})
        for key, filed_key in TRACK_PIECES:
            val = (note if key == "note" else (t.get(key) or "")).strip()
            if not val:
                continue
            was = now.get(filed_key)
            props.append({"field": "track.%d.%s" % (t["number"], key), "current": was or "", "proposed": val,
                          "confidence": "medium", "source": "web", "agrees": _same(val, was)})
    # An answer exists only for a question that was asked; otherwise nothing refers to one.
    answer = raw.get("answer", "") if asked else ""
    thinking = raw.get("thinking", "")
    if not asked:
        thinking = filters.drop_absent_question_sentences(thinking)
    result = {"thinking": thinking, "answer": answer,
              "proposals": props, "tracks": tracks,
              "track_titles": [{"number": t["number"], "title": t["title"]} for t in tracks if t["title"]],
              "verify_items": raw.get("verify_items") or [],
              "provenance_notes": raw.get("provenance_notes") or [],
              "sources": raw.get("sources") or [], "scope": sorted(scope)}
    return result, props


SKILL = Skill(key="recording", subject_types=("recording", "folder"), instructions=INSTRUCTIONS,
              submit_tool=SUBMIT, gather=gather, normalize=normalize, base_tokens=8000, label="Recording")
