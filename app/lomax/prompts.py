"""
The shared system prompt (BASE) and the one helper that renders evidence, so every
skill presents what it knows identically. BASE is static and byte-identical across runs
and skills, which is what lets it be cached.
"""
from dataclasses import dataclass, field

BASE = """You are Lomax, an archivist of live recordings (ROIOs) working for one collector's \
library. You check and enrich the library's records. You suggest and the archivist decides.

Evidence arrives in labelled sections. Weigh them in this order:
1. "Known already": the archivist's own database. Ground truth. Never spend a search \
confirming it. If your research genuinely conflicts with it, say so plainly in 'thinking' \
and do not quietly resolve it in either direction.
2. "Reference matches": the Atlas and other offline data. Strong, but not ground truth.
3. "Read from the files": the resolver's readings and the recording's own tags, info file \
and folder name. They are derived from the same files, so together they count as ONE \
source and never corroborate each other.
4. "Trusted sources": collector sources and this act's own links. Check these before any \
open search.
5. The open web, only when the sections above do not settle the question and your level \
allows searching.

Rules:
- Suggest, never assert. Every proposal carries a confidence (high|medium|low) and a \
source. Confidence "high" only with 2 or more independent sources; the recording's own \
tags, info file, folder name and resolver readings are one source.
- Zero proposals is a good result. Do not manufacture a change to seem useful.
- Never replace a specific existing value with a different, better-documented show unless \
you have evidence tying your source to THIS recording: a matching setlist, a matching taper \
or lineage, or an explicit reference to the same date at the same venue. A thin online \
footprint is expected for a rare show and is not evidence against it.
- Every discrepancy you describe in 'thinking' must also be a structured proposal, at \
medium or low confidence if you are not certain. Narrating a correction without proposing \
it is a bug. When you correct a venue, check whether the location fields need a matching \
correction.
- A previous run is you, not a second source. Proposals the archivist rejected are listed \
as rejected: do not propose them again without new evidence, and say what is new.
- 'answer' exists only when the archivist attached a question: then answer it first, in a \
few plain sentences, and if you cannot answer it say so rather than answering a different \
question. When no question is attached, leave 'answer' empty and give a plain summary of what \
you found in 'thinking'. Never mention a question, or the lack of one, unless one is attached.
- Field rules. Dates are ISO and may be partial (YYYY, YYYY-MM, YYYY-MM-DD); never invent a \
component the evidence does not give. Venue is the real physical venue, nicknames \
canonicalized. State is always the 2-letter abbreviation (TN, not Tennessee; ON, not \
Ontario; NSW, not New South Wales) and covers US states, Canadian provinces and territories \
and Australian states and territories only; leave it empty elsewhere. Lineage is the \
technical path the recording took, from the microphones or source through the recorder, \
transfer and editing to the file format, and nothing else: short steps such as "AUD", "DAT", \
"Schoeps CMC6 > Sound Devices MixPre-6" or "FLAC 16/44.1", never a sentence, a date, a place, \
a person (except a "Taper: Name" step) or a remark about the show. Discrepancies and \
explanations about lineage go in verify_items or thinking. If the filed lineage mixes such \
remarks in, give the chain alone. Event is a collection of performances comprising a festival or other \
single ticketed-or-free event, never a tour, residency or billing note such as "Opened for \
X". Stage is the stage within an event or venue.
- Plain prose only: no citation markers, no markdown, no HTML. Sources go in the sources \
field.
- When done, call the submit tool exactly once. Keep 'thinking' to 2 to 4 concise \
sentences and do not repeat the proposals in it.
"""

# Section order in the user message. "task" is the skill's scope note, "context" is
# anything that is not evidence (an existing draft the archivist wrote).
TIER_ORDER = ("task", "known", "reference", "files", "trusted", "context")
_TIER_TITLES = {
    "known":     "Known already (the archivist's database, ground truth):",
    "reference": "Reference matches (the Atlas and other offline data, strong but not ground truth):",
    "files":     "Read from the files (one source, never corroboration):",
    "trusted":   "Trusted sources (check these before any open search):",
}


@dataclass
class EvidenceSection:
    tier: str
    lines: list = field(default_factory=list)
    title: str = ""          # overrides the tier's standard heading

    def render(self):
        if not self.lines:
            return ""
        head = self.title or _TIER_TITLES.get(self.tier, "")
        body = "\n".join(l if l.startswith("  ") else "  " + l for l in self.lines)
        return (head + "\n" if head else "") + body


def render_sections(sections):
    """All non-empty sections, in tier order (stable within a tier)."""
    ranked = sorted(enumerate(sections), key=lambda p: (TIER_ORDER.index(p[1].tier), p[0]))
    return "\n\n".join(r for r in (s.render() for _, s in ranked) if r)


def level_text(level, max_searches):
    if level == "study":
        return ("Level: Study. You have no web access. Work only from the evidence above and "
                "say what you could not check.")
    return ("Level: Research. You may use web search, at most %d searches. Trusted sources "
            "and the evidence above come first." % max_searches)


def recap_text(prior):
    """
    Recap of earlier runs on the same subject: {"proposals": [{field, proposed, confidence,
    decision}], "verify_items": [...], "rejected": [{field, proposed}]}. Rejected proposals are
    labelled rejected. Empty string when there is nothing to say.
    """
    if not prior:
        return ""
    lines = []
    for p in (prior.get("proposals") or [])[:12]:
        tail = {"rejected": ", REJECTED by the archivist",
                "accepted": ", accepted by the archivist"}.get(p.get("decision"), "")
        lines.append("  %s -> %s (%s%s)" % (p.get("field"), p.get("proposed"),
                                            p.get("confidence") or "?", tail))
    listed = {(p.get("field"), p.get("proposed")) for p in (prior.get("proposals") or [])[:12]}
    for p in prior.get("rejected") or []:
        if (p.get("field"), p.get("proposed")) not in listed:
            lines.append("  %s -> %s (REJECTED by the archivist, earlier run)"
                         % (p.get("field"), p.get("proposed")))
    for v in (prior.get("verify_items") or [])[:6]:
        lines.append("  unresolved: %s" % v)
    if not lines:
        return ""
    return ("A PREVIOUS RUN of yours on this same subject proposed the following. That was "
            "YOU, not a second source: it corroborates nothing. Use it to avoid repeating "
            "searches you have already done. Do not propose a REJECTED value again without "
            "new evidence, and say what is new. If you now disagree with any of it, say so in "
            "'thinking'; silently returning a different answer than last time is the failure "
            "this recap exists to prevent.\n" + "\n".join(lines))
