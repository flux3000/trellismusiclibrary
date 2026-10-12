"""Musician Biography: one person's biography, with the acts they played with written into it."""
from app.extensions import db
from app.lomax.prompts import EvidenceSection
from app.lomax.skills.base import REPLY_PROP, Skill
from app.lomax.sources import trusted_section

INSTRUCTIONS = """SKILL: Musician Biography.
Research one musician for an archivist's reference page: a short biography.
- Biography: a few concise paragraphs on who the person is, instruments, where they came \
from and the arc of their career. Write the bands and acts they played with INTO the \
prose, with years where your source gives them. Never list them separately and never \
propose them as records. Ground it in what your evidence supports and never invent \
unverifiable specifics. If the evidence is thin, write a shorter, honest biography. \
Separate paragraphs with a blank line and nothing else.
- Use the library's own records (acts they are on the roster of, recordings) as ground truth \
for which acts to name; add others only when a source supports them.
- Answer free-form questions in 'answer'."""

SUBMIT = {
    "name": "submit_musician_biography",
    "description": "Submit the musician's biography.",
    "input_schema": {
        "type": "object",
        "properties": {
            "thinking": {"type": "string"},
            "answer": {"type": "string", "description": "Direct answer to the archivist's question, if one was asked."},
            "reply": REPLY_PROP,
            "biography": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "url": {"type": "string"}}}},
        },
        "required": ["thinking", "biography"],
    },
}


def gather(subject, ctx):
    m = subject.obj
    lines = ["Name: " + m.name]
    acts = []
    for ms in m.memberships:
        if ms.artist and ms.artist.name not in acts:
            acts.append(ms.artist.name)
    if acts:
        lines.append("Acts on the record: " + ", ".join(acts[:20]))
    secs = [EvidenceSection("task", ["Musician: " + m.name]), EvidenceSection("known", lines),
            trusted_section(None)]
    if (m.bio or "").strip():
        secs.append(EvidenceSection("context", [m.bio.strip()],
                                    title="Existing biography draft (may be incomplete or outdated):"))
    return secs


def normalize(raw, subject):
    result = {"thinking": raw.get("thinking", ""), "answer": raw.get("answer", ""),
              "reply": raw.get("reply", ""), "biography": raw.get("biography", ""),
              "proposals": [], "sources": raw.get("sources") or []}
    return result, []


def apply_auto(subject, result):
    """Like the artist biography: a non-empty biography replaces musician.bio; the text it
    replaced is kept on the run for Restore Previous."""
    bio = (result.get("biography") or "").strip()
    if not bio:
        return None
    before = subject.obj.bio or ""
    subject.obj.bio = bio
    db.session.commit()
    return {"biography": True, "replaced": {"field": "bio", "text": before, "written": bio}}


SKILL = Skill(key="musician", subject_types=("musician",), instructions=INSTRUCTIONS, submit_tool=SUBMIT,
              gather=gather, normalize=normalize, apply_auto=apply_auto, base_tokens=2500,
              label="Musician Biography")
