"""Venue History: what a venue was, what it was called, where it is."""
import json

from app.extensions import db
from app.lomax import evidence as ev
from app.lomax.prompts import EvidenceSection
from app.lomax.skills.base import Skill
from app.lomax.sources import trusted_section

INSTRUCTIONS = """SKILL: Venue History.
Research one venue for an archivist's reference page: a short history, the names it has \
carried, and whether its location is right.
- History: a short plain-prose paragraph or two. What the room was, when it opened and \
closed or changed hands, why collectors care. Say less rather than guess.
- former_names: one entry per earlier name, with 'from' and 'to' years where your source \
gives them and a 'url'. Names under "Reference matches" with years are already known; add \
only what the evidence supports beyond them.
- Location: propose city, state or country only when the filed value looks wrong, as \
proposals with a confidence and source. Zero proposals is a good result.
- resources: links to pages about the venue, with a label and a url."""

SUBMIT = {
    "name": "submit_venue_history",
    "description": "Submit the venue's history, former names and any location corrections.",
    "input_schema": {
        "type": "object",
        "properties": {
            "thinking": {"type": "string"},
            "answer": {"type": "string", "description": "Direct answer to the archivist's question, if one was asked."},
            "history": {"type": "string"},
            "former_names": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "from": {"type": "string"}, "to": {"type": "string"},
                "url": {"type": "string"}}, "required": ["name"]}},
            "proposals": {"type": "array", "items": {"type": "object", "properties": {
                "field": {"type": "string", "enum": ["city", "state", "country"]},
                "current": {"type": "string"},
                "proposed": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "source": {"type": "string", "enum": ["web", "trusted", "atlas", "db_match"]},
                "url": {"type": "string"}},
                "required": ["field", "proposed", "confidence", "source"]}},
            "resources": {"type": "array", "items": {"type": "object", "properties": {
                "label": {"type": "string"}, "url": {"type": "string"}}, "required": ["label", "url"]}},
            "sources": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "url": {"type": "string"}}}},
        },
        "required": ["thinking", "history", "proposals"],
    },
}


def gather(subject, ctx):
    v = subject.obj
    return [EvidenceSection("task", ["Venue: " + v.name]),
            ev.venue_known_section(v), ev.venue_atlas_section(v), trusted_section(None)]


def normalize(raw, subject):
    props = [p for p in (raw.get("proposals") or [])
             if isinstance(p, dict) and p.get("field") in ("city", "state", "country") and p.get("proposed")]
    former = [n for n in (raw.get("former_names") or []) if isinstance(n, dict) and n.get("name")]
    result = {"thinking": raw.get("thinking", ""), "answer": raw.get("answer", ""),
              "history": raw.get("history", ""),
              "former_names": former,
              "proposals": props,
              "resources": [r for r in (raw.get("resources") or []) if isinstance(r, dict) and r.get("url")],
              "sources": raw.get("sources") or []}
    # A former name is a proposal too, so "Add as alias" and "Dismiss" are recorded like any other.
    names = props + [{"field": "former_name", "proposed": json.dumps(n), "url": n.get("url")} for n in former]
    return result, names


def apply_auto(subject, result):
    """Like the artist biography: a non-empty history is written to venue.history. The text it
    replaced is kept on the run so the archivist can put it back (POST /runs/<id>/restore)."""
    text = (result.get("history") or "").strip()
    if not text:
        return None
    before = subject.obj.history or ""
    subject.obj.history = text
    db.session.commit()
    return {"history": True, "replaced": {"field": "history", "text": before, "written": text}}


SKILL = Skill(key="venue", subject_types=("venue",), instructions=INSTRUCTIONS, submit_tool=SUBMIT,
              gather=gather, normalize=normalize, apply_auto=apply_auto, base_tokens=2500, label="Venue History")
