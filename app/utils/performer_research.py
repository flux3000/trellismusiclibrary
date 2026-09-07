"""
app/utils/performer_research.py — AI Dossier research for the Performer page.

run_performer_research() sends an act's name (+ any existing bio draft) to
Anthropic (BYOK key, same model preference as ingest-side AI Assist), lets it
research with the web-search tool, and returns a drafted biography plus
suggested external resource links (collector sites, discography databases,
etc.) for human review. Nothing is written to the Performer record —
the caller (frontend) reviews and applies: the bio is copy-paste into `bio`,
each resource link is an individual "Add" action into the Resources list.

Same "AI suggests, human approves" rule as app/utils/ai_assist.py — see the
AI Assist Refinement spec (Context Library, 2026-07-20/21): auto-apply was
deliberately removed there after a wrong-but-confident result silently
overwrote a recording's date. Nothing here auto-applies either.
"""

import os
import re
import time

from app.utils.ai_assist import (AiAssistError, _usage_summary,
                                 MAX_SEARCHES, MAX_SEARCHES_WITH_QUESTION)


def _log(msg):
    print("[dossier] " + msg, flush=True)

# Guarded import so the module loads even when the SDK isn't installed yet —
# same pattern as app/utils/ai_assist.py.
try:
    import anthropic
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False


_GROUNDING = """
Ground rules about what you are given:
- Anything under "Known already" comes from the archivist's own database, some of it
from MusicBrainz. TREAT IT AS GROUND TRUTH. Do not contradict it, do not re-derive it,
and do not spend a search confirming it — searches are the expensive part of this job.
Use it to disambiguate: acts share names, and knowing the origin, active years and
aliases is usually what tells two of them apart.
- If your research genuinely conflicts with something under "Known already", say so
plainly in 'thinking'. Do not quietly resolve it in either direction.
- If the human attached a question, answer it in 'answer' — directly, in a few plain
sentences. A question is the human telling you where the effort is wanted. If you
cannot answer it, say so rather than answering a different question.
"""

_BIO_SYSTEM = """You are the Trellis Research Assistant, researching a live-music \
performing act for an archivist's reference page (this is a library of live concert \
recordings — ROIOs — not a commercial streaming catalog).

Rules:
- Write a biography: a few concise paragraphs — formation, key lineup history, era/genre,
notable characteristics as a live act. Grounded in what your research actually supports;
never invent unverifiable specifics (exact dates, member names, etc.) just to sound complete.
If your research is thin, write a shorter, honest biography rather than padding it.
- PLAIN PROSE ONLY in the biography. No citation markers, no footnote brackets like [1],
no markdown, no HTML, no <cite> tags. Separate paragraphs with a blank line and nothing
else. Cited sources belong in the 'sources' field, never inline in the text.
- Suggest external resource links: prioritize collector/taper community sites, discography
or setlist databases (setlist.fm, etree, archive.org, a fan-maintained "known shows"
database if one exists for this act), and dedicated fan archives. These are the kind of
links useful to someone cataloging live recordings — not generic Wikipedia/streaming
service links unless nothing more specific exists for this act.
- Every resource link needs a label (what it is, e.g. "Setlist database") and a url.
- When done, call submit_dossier exactly once with your findings. Keep 'thinking' to
1-2 sentences — what you found and how confident you are, not a repeat of the biography.
""" + _GROUNDING


# Lineup research is its own pass, its own button and its own prompt — NOT a
# section bolted onto the biography (Ryan, 2026-09-07). Two reasons: it is far
# more search-hungry than a bio (a forty-year band is a lot of roster churn),
# and the bio pass OVERWRITES the description as a deliberate exception to
# approve-first, so folding lineup into it would mean anyone wanting tenure
# dates had to accept a rewritten description they never asked for.
_LINEUP_SYSTEM = """You are the Trellis Research Assistant, researching the LINEUP \
HISTORY of a live-music performing act for an archivist's reference page.

Your single job is to establish WHO was in this act and WHEN. Nothing else.

Rules:
- Return one entry per person per continuous STINT. A person who left and later
rejoined gets TWO entries, not one spanning the gap — that gap is the whole reason this
data is worth having, because it is what decides whether someone was on stage at a
given show.
- Dates are partial ISO: "1974", "1974-10", or "1974-10-19". Give only the precision
your source actually supports. A year you are sure of beats a fabricated exact date.
Leave 'end' empty for a current member. Leave BOTH empty only for someone who has been
in the act for its entire existence.
- Every entry needs a 'url' pointing at the specific source for THAT person's dates.
An entry with no url will be shown to the human as unusable, so do not pad the list
with people whose tenure you cannot source.
- 'instrument' is what they played during that stint, comma-separated free text
("fiddle, banjo"). Omit it rather than guessing.
- Confidence is about the DATES, not about whether the person was ever in the act.
"Certainly a member, dates disputed" is medium or low, not high.
- Prefer the act's own site, a fan-maintained roster or discography, or an established
database over a general encyclopedia article, and say in 'thinking' where the roster
mostly came from.
- Sources routinely disagree about tenure dates. When they do, give the entry your best
supported dates at LOW confidence and put the disagreement in 'notes' for that entry.
Do not average two sources into a date neither of them states.
- Never invent a person. A short, sourced roster is worth more than a long, guessed one.
- When done, call submit_lineup exactly once.
""" + _GROUNDING

_SUBMIT_TOOL = {
    "name": "submit_dossier",
    "description": "Submit your biography draft and suggested resource links for human review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "thinking":  {"type": "string", "description": "Brief reasoning/confidence note."},
            "answer":    {"type": "string",
                          "description": "Direct answer to the human's question, if one was asked."},
            "biography": {"type": "string", "description": "Drafted biography, a few paragraphs."},
            "resources": {
                "type": "array",
                "description": "Suggested external resource links.",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "url":   {"type": "string"},
                    },
                    "required": ["label", "url"],
                },
            },
            "sources": {
                "type": "array",
                "items": {"type": "object",
                          "properties": {"title": {"type": "string"}, "url": {"type": "string"}}},
            },
        },
        "required": ["thinking", "biography"],
    },
}

# Shape mirrors the Membership model deliberately: one entry = one row, partial
# y/m/d bounds, free-text instrument. Nothing here auto-applies — see the
# module docstring — so the human is reading these rows and clicking each one
# they want. `url` is required per entry for that reason: a row the human
# cannot check is a row they cannot responsibly accept.
_SUBMIT_LINEUP = {
    "name": "submit_lineup",
    "description": "Submit the act's researched lineup history for human review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "thinking": {"type": "string", "description": "Where the roster came from, 1-2 sentences."},
            "answer":   {"type": "string",
                         "description": "Direct answer to the human's question, if one was asked."},
            "members": {
                "type": "array",
                "description": "One entry per person per continuous stint.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name":       {"type": "string"},
                        "instrument": {"type": "string"},
                        "start":      {"type": "string", "description": "Partial ISO: 1974, 1974-10, 1974-10-19."},
                        "end":        {"type": "string", "description": "Partial ISO. Empty if current."},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "note":       {"type": "string", "description": "Source disagreement or caveat."},
                        "url":        {"type": "string"},
                    },
                    "required": ["name", "confidence"],
                },
            },
            "sources": {
                "type": "array",
                "items": {"type": "object",
                          "properties": {"title": {"type": "string"}, "url": {"type": "string"}}},
            },
        },
        "required": ["thinking", "members"],
    },
}

_MODES = {
    "bio":    (_BIO_SYSTEM,    _SUBMIT_TOOL,   "submit_dossier"),
    "lineup": (_LINEUP_SYSTEM, _SUBMIT_LINEUP, "submit_lineup"),
}


def _context_block(context):
    """
    Render the "Known already" section from what the DB already holds.

    This exists because the Dossier used to research an act completely blind
    while the record beside it held the roster, the MusicBrainz origin/active
    years and every known alias (2026-09-07). That cost real searches
    re-deriving facts we had, and it made same-name acts a coin flip.
    """
    context = context or {}
    lines = []
    if context.get("aliases"):
        lines.append("Also known as: %s" % ", ".join(context["aliases"][:8]))
    for label, key in (("Type", "mb_type"), ("Origin", "mb_area"),
                       ("Active from", "mb_begin"), ("Active until", "mb_end"),
                       ("Genre", "genre"), ("Disambiguation", "mb_disambiguation")):
        if context.get(key):
            lines.append("%s: %s" % (label, context[key]))
    members = context.get("members") or []
    if members:
        lines.append("Members already on the record (%d): %s"
                     % (len(members), ", ".join(members[:40])))
    if not lines:
        return ""
    return "Known already (from the archivist's database — ground truth):\n  " \
        + "\n  ".join(lines) + "\n"


# Citation markup the web-search tool leaves in generated prose. It surfaced as
# literal "<cite index=...>" text in the biography on the Performer page
# (2026-08-07). Stripped SERVER-SIDE so what we store is already clean —
# scrubbing at render time would leave the mess in `dossier_json` forever and
# require every future consumer to re-implement the same cleanup.
_CITE_RE = re.compile(r"</?cite[^>]*>", re.IGNORECASE)
# Bare bracketed reference markers: [1], [12], [1,2], [3][4]
_REF_RE = re.compile(r"\[\s*\d+(?:\s*[,–-]\s*\d+)*\s*\]")
_WS_RE = re.compile(r"[ \t]{2,}")


def _clean_prose(text):
    """Strip citation markup and tidy whitespace, preserving paragraph breaks."""
    if not text:
        return text
    out = _CITE_RE.sub("", text)
    out = _REF_RE.sub("", out)
    # Punctuation left stranded by a removed marker: "word ." / "word ,"
    out = re.sub(r"\s+([.,;:!?])", r"\1", out)
    out = _WS_RE.sub(" ", out)
    # Collapse 3+ newlines to a paragraph break; keep single/double as authored.
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _extract_result(resp, tool_name):
    """Pull the submit tool's input from the response; fall back to prose."""
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            data = dict(block.input)
            for key in ("biography", "thinking", "answer"):
                if data.get(key):
                    data[key] = _clean_prose(data[key])
            return data
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
    return {"thinking": _clean_prose(text) or "No structured result returned.",
            "biography": "", "resources": [], "members": [], "sources": []}


def run_performer_research(performer_name, current_bio, api_key, model,
                           *, context=None, question=None, mode="bio"):
    """
    Run a research pass over an act. Two modes, one call path:

      mode="bio"    → {thinking, answer, biography, resources, sources, usage, model}
      mode="lineup" → {thinking, answer, members, sources, usage, model}

    `context` is what the DB already knows (see _context_block); `question` is
    the human's optional pre-run question. Raises AiAssistError on any
    recoverable failure (reused from ai_assist.py — same class of failure, no
    reason for a second exception type).
    """
    if not _HAS_SDK:
        raise AiAssistError("The 'anthropic' package is not installed. Run: pip install anthropic")
    if not api_key:
        raise AiAssistError("No Anthropic API key configured.")
    if mode not in _MODES:
        raise AiAssistError("Unknown research mode %r" % mode)
    system, submit_tool, tool_name = _MODES[mode]

    text = "Act name: %s\n" % performer_name
    ctx = _context_block(context)
    if ctx:
        text += "\n" + ctx
    if mode == "bio" and (current_bio or "").strip():
        text += "\nExisting bio draft (may be incomplete or outdated):\n%s\n" % current_bio.strip()
    question = (question or "").strip()
    if question:
        text += "\nThe archivist asks: %s\n" % question
    text += ("\nResearch this act's lineup history and call submit_lineup with your findings."
             if mode == "lineup" else
             "\nResearch this act and call submit_dossier with your findings.")

    # A question earns a bigger search budget — see MAX_SEARCHES in ai_assist.py.
    max_uses = MAX_SEARCHES_WITH_QUESTION if question else MAX_SEARCHES

    _log("start act=%r mode=%s model=%s q=%s max_uses=%d"
         % (performer_name, mode, model, bool(question), max_uses))

    client = anthropic.Anthropic(api_key=api_key, timeout=300.0)
    t0 = time.time()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            # cache_control on the LAST system block caches everything above it.
            # The system prompt is static and identical across every run, so
            # after the first pass in a five-minute window it is billed at a
            # tenth of the input rate. Small today; the reason it is here now
            # is that it must exist before any multi-turn feature does.
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            tools=[
                {"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses},
                submit_tool,
            ],
            messages=[{"role": "user", "content": text}],
        )
    except anthropic.AuthenticationError:
        raise AiAssistError("Anthropic rejected the API key (check it in Settings).")
    except anthropic.APITimeoutError:
        raise AiAssistError("Anthropic request timed out (research took too long).")
    except anthropic.APIError as e:
        _log("APIError: %s" % getattr(e, "message", str(e)))
        raise AiAssistError("Anthropic API error: %s" % getattr(e, "message", str(e)))

    _log("anthropic returned in %.1fs stop_reason=%s" % (time.time() - t0, getattr(resp, "stop_reason", None)))
    result = _extract_result(resp, tool_name)
    result.setdefault("resources", [])
    result.setdefault("members", [])
    result.setdefault("sources", [])
    result["mode"] = mode
    result["model"] = model

    usage = _usage_summary(getattr(resp, "usage", None))
    result["usage"] = usage
    if usage:
        _log("usage: in=%d out=%d searches=%d"
             % (usage["input_tokens"], usage["output_tokens"],
                usage["web_search_requests"]))
    return result
