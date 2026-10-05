"""Artist History: biography, lineup stints and resource links in one pass."""
import json

from app.extensions import db
from app.lomax import evidence as ev
from app.lomax.prompts import EvidenceSection
from app.lomax.skills.base import Skill
from app.lomax.sources import trusted_section

INSTRUCTIONS = """SKILL: Artist History.
One pass over a live-music performing act, for an archivist's reference page. Produce its \
biography, its lineup history and its resource links together. This is a library of live \
concert recordings, not a commercial streaming catalog.
- Biography: a few concise paragraphs on formation, key lineup history, era and genre, and \
what marks the act live. Ground it in what your evidence supports and never invent \
unverifiable specifics. If the evidence is thin, write a shorter, honest biography. Separate \
paragraphs with a blank line and nothing else.
- Lineup: one entry per person per continuous STINT. A person who left and later rejoined \
gets TWO entries; the gap is what decides whether someone was on stage at a given show. \
Dates are partial ISO and carry only the precision your source supports; a year you are sure \
of beats a fabricated exact date. Leave 'end' empty for a current member. Leave both dates \
empty only for someone in the act for its entire existence. 'instrument' is what they played \
during that stint, comma-separated. Every entry needs a 'url' for THAT person's dates; do not \
pad the list with people whose tenure you cannot source. Confidence is about the DATES: \
"certainly a member, dates disputed" is medium or low. When sources disagree about dates, \
give your best supported dates at low confidence and put the disagreement in 'note'. Never \
average two sources into a date neither states. Never invent a person.
- Resources: external links useful to someone cataloging live recordings: collector and taper \
sites, setlist or show databases, fan archives. Skip generic encyclopedia and streaming \
links unless nothing more specific exists. Every link needs a label and a url, and must not \
repeat a link already on the record.
- Say in 'thinking' where the roster mostly came from."""

SUBMIT = {
    "name": "submit_artist_history",
    "description": "Submit the act's biography, lineup history and resource links.",
    "input_schema": {
        "type": "object",
        "properties": {
            "thinking": {"type": "string"},
            "answer": {"type": "string", "description": "Direct answer to the archivist's question, if one was asked."},
            "biography": {"type": "string"},
            "members": {"type": "array", "description": "One entry per person per continuous stint.",
                        "items": {"type": "object", "properties": {
                            "name": {"type": "string"}, "instrument": {"type": "string"},
                            "start": {"type": "string", "description": "Partial ISO: 1974, 1974-10, 1974-10-19."},
                            "end": {"type": "string", "description": "Partial ISO. Empty if current."},
                            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                            "note": {"type": "string"}, "url": {"type": "string"}},
                            "required": ["name", "confidence"]}},
            "resources": {"type": "array", "items": {"type": "object", "properties": {
                "label": {"type": "string"}, "url": {"type": "string"}}, "required": ["label", "url"]}},
            "sources": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "url": {"type": "string"}}}},
        },
        "required": ["thinking", "biography"],
    },
}


def gather(subject, ctx):
    a = subject.obj
    secs = [ev.artist_known_section(a), trusted_section(a)]
    if (a.bio or "").strip():
        secs.append(EvidenceSection("context", [a.bio.strip()],
                                    title="Existing biography draft (may be incomplete or outdated):"))
    return [EvidenceSection("task", ["Act name: " + a.name])] + secs


def normalize(raw, subject):
    members = [m for m in (raw.get("members") or []) if isinstance(m, dict) and m.get("name")]
    resources = [r for r in (raw.get("resources") or []) if isinstance(r, dict) and r.get("url")]
    result = {"thinking": raw.get("thinking", ""), "answer": raw.get("answer", ""),
              "biography": raw.get("biography", ""), "members": members, "resources": resources,
              "sources": raw.get("sources") or []}
    props = ([{"field": "member", "proposed": json.dumps(m), "confidence": m.get("confidence"),
               "url": m.get("url")} for m in members]
             + [{"field": "resource", "proposed": json.dumps(r), "url": r.get("url")} for r in resources])
    return result, props


def apply_auto(subject, result):
    """The one auto-apply (CONTEXT.md): a non-empty biography replaces the description. It is
    visible the moment it lands, freely editable, and nothing else computes from it."""
    bio = (result.get("biography") or "").strip()
    if not bio:
        return None
    before = subject.obj.bio or ""
    subject.obj.bio = bio
    db.session.commit()
    return {"biography": True, "replaced": {"field": "bio", "text": before, "written": bio}}


SKILL = Skill(key="artist", subject_types=("artist",), instructions=INSTRUCTIONS, submit_tool=SUBMIT,
              gather=gather, normalize=normalize, apply_auto=apply_auto, base_tokens=3000,
              label="Artist History")
