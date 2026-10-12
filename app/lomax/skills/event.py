"""Event History: what an event was, when and where it ran, and who played it."""
from app.extensions import db
from app.lomax import filters
from app.lomax.evidence import split_iso
from app.lomax.prompts import EvidenceSection
from app.lomax.skills.base import REPLY_PROP, Skill
from app.lomax.sources import trusted_section
from app.utils.format import format_partial_date

INSTRUCTIONS = """SKILL: Event History.
Research one event (a festival or other single ticketed-or-free event) for an archivist's \
reference page: a short history, its dates, its location and its lineup.
- History: a short plain-prose paragraph or two. What the event was, who organized it, when \
and where it ran, why collectors care. Say less rather than guess.
- Dates and location: propose start_date, end_date, city, state or country only when the \
filed value is missing or looks wrong, as proposals with a confidence and source. Dates are \
partial ISO (YYYY, YYYY-MM, YYYY-MM-DD). State is the 2-letter abbreviation (TN, not \
Tennessee). Zero proposals is a good result.
- Lineup: the names of the acts that played, as plain names only. This is reference data \
shown in the chat; it is never applied to the record. Do not pad it with acts you cannot \
source.
- Answer free-form questions in 'answer'."""

SUBMIT = {
    "name": "submit_event_history",
    "description": "Submit the event's history, date and location proposals and lineup.",
    "input_schema": {
        "type": "object",
        "properties": {
            "thinking": {"type": "string"},
            "answer": {"type": "string", "description": "Direct answer to the archivist's question, if one was asked."},
            "reply": REPLY_PROP,
            "history": {"type": "string"},
            "proposals": {"type": "array", "items": {"type": "object", "properties": {
                "field": {"type": "string", "enum": ["start_date", "end_date", "city", "state", "country"]},
                "current": {"type": "string"},
                "proposed": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "source": {"type": "string", "enum": ["web", "trusted", "atlas", "db_match"]},
                "url": {"type": "string"}},
                "required": ["field", "proposed", "confidence", "source"]}},
            "lineup": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "url": {"type": "string"}}}},
        },
        "required": ["thinking", "history", "proposals"],
    },
}

_FIELDS = ("start_date", "end_date", "city", "state", "country")


def current_value(event, field):
    if field == "start_date":
        return format_partial_date(event.start_year, event.start_month, event.start_day) or ""
    if field == "end_date":
        return format_partial_date(event.end_year, event.end_month, event.end_day) or ""
    return getattr(event, field) or ""


def gather(subject, ctx):
    e = subject.obj
    lines = ["Name: " + e.name]
    for label, f in (("Start date", "start_date"), ("End date", "end_date"),
                     ("City", "city"), ("State", "state"), ("Country", "country")):
        v = current_value(e, f)
        if v:
            lines.append("%s: %s" % (label, v))
    if e.venue:
        lines.append("Venue: " + e.venue.name)
    acts = sorted({p.artist.name for p in e.performances if p.artist})
    if acts:
        lines.append("Acts recorded at this event in the library: " + ", ".join(acts[:30]))
    return [EvidenceSection("task", ["Event: " + e.name]), EvidenceSection("known", lines), trusted_section(None)]


def normalize(raw, subject):
    e = subject.obj
    props = []
    for p in raw.get("proposals") or []:
        if not (isinstance(p, dict) and p.get("field") in _FIELDS and p.get("proposed")):
            continue
        f, v = p["field"], str(p["proposed"]).strip()
        if f in ("start_date", "end_date"):
            if not split_iso(v):
                continue
        elif f == "state":
            v = filters.state_code(v, e.country)
        props.append(dict(p, proposed=v, current=current_value(e, f) or None,
                          agrees=filters.same_text(v, current_value(e, f))))
    lineup = []
    for n in raw.get("lineup") or []:
        n = (n.get("name") if isinstance(n, dict) else n)
        n = str(n or "").strip()
        if n and n.lower() not in {x.lower() for x in lineup}:
            lineup.append(n)
    result = {"thinking": raw.get("thinking", ""), "answer": raw.get("answer", ""),
              "reply": raw.get("reply", ""), "history": raw.get("history", ""),
              "proposals": props, "lineup": lineup, "sources": raw.get("sources") or []}
    return result, props


def apply_auto(subject, result):
    """Like the venue history: a non-empty history is written to event.history, and the text it
    replaced is kept on the run for Restore Previous."""
    text = (result.get("history") or "").strip()
    if not text:
        return None
    before = subject.obj.history or ""
    subject.obj.history = text
    db.session.commit()
    return {"history": True, "replaced": {"field": "history", "text": before, "written": text}}


SKILL = Skill(key="event", subject_types=("event",), instructions=INSTRUCTIONS, submit_tool=SUBMIT,
              gather=gather, normalize=normalize, apply_auto=apply_auto, base_tokens=2500, label="Event History")
