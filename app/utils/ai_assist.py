"""
app/utils/ai_assist.py — AI Research Assistant for the Add Recording flow.

run_ai_assist() sends a scanned recording's current metadata (and the info-file
text, if there is one) to Anthropic (BYOK key, Sonnet 5 by default), lets the
model research with the web-search tool, and returns structured, source-cited
proposals for human review. Nothing is written — the caller (frontend) reviews
and applies.

NOTE: this docstring used to claim it also sent "any bundled poster/flyer
images." It never did — no image content block has ever been constructed here.
Corrected 2026-09-07 rather than built, because nobody asked for it. Worth
building later: a poster photograph is a genuine primary source for exactly the
rare, thin-footprint show that the confidence rules below exist to protect.

Design rules baked into the system prompt:
  • "AI suggests, human approves" — never assert; every proposal carries a
    confidence + source (+ url).
  • confidence "high" ONLY when corroborated by >=2 INDEPENDENT sources or a
    primary-source image (poster/flyer). Internal tags/info/DB all trace to one
    origin — they are NOT independent of each other.
  • Scalars (venue/city/state/country/date/source/event) are proposals and may be
    auto-applied by the UI when high-confidence. Track titles / setlist are NEVER
    proposals — setlist problems go to verify_items for human-by-ear resolution.
  • A finding described in 'thinking' but missing from 'proposals' is a bug (seen
    2026-07-14: model found a well-corroborated date/venue discrepancy, wrote it up
    in the narrative, and returned zero proposals). The prompt now explicitly
    requires every narrated discrepancy to have a matching proposal, even at low
    confidence — the UI never auto-applies below high, so there's no reason to
    withhold one.
  • Notes split: verify_items (transient, → DB Notes) vs provenance_notes
    (lasting, → info-file). ISO dates. Location = city[, state][, country], US only
    for state.
"""

import os
import time


def _log(msg):
    """Print to the Flask console (visible in the terminal running run.py)."""
    print("[ai-assist] " + msg, flush=True)

# Guarded import so the module loads even when the SDK isn't installed yet.
try:
    import anthropic
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False


class AiAssistError(Exception):
    """Raised for any recoverable failure in the AI pass (surfaced to the UI)."""


# Scalar fields the model may propose (and the UI may auto-apply). Track titles
# are deliberately excluded — they are suggestion-only via verify_items.
# `lineage` was sent to the model from the start but was missing from this list,
# so for a year the model could READ a wrong lineage and had no way to say so
# (2026-09-07). Added.
_PROPOSAL_FIELDS = ["artist", "date", "venue", "city", "state", "country",
                    "source", "lineage", "event"]

# ── Web-search budget ───────────────────────────────────────────────────────
# Search is ~95% of the token bill (~15k input tokens per search, measured) and
# it is worse than linear: every result stays in the context window for every
# later turn of the SAME run, so the sixth search is paid for five more times
# than the first. 4 is the default. A run carrying a specific question from the
# human gets 6, because a question is evidence the extra digging is wanted —
# effort follows intent rather than being spent by default (Ryan, 2026-09-07).
MAX_SEARCHES = 4
MAX_SEARCHES_WITH_QUESTION = 6

# Info-file text is sent verbatim and is normally tiny (~1KB across the library),
# but a 28KB file exists in the Backlog today (~7k tokens). Cap it so one
# pathological file cannot quietly triple the cost of a pass. The cap is far
# above the real distribution, so in practice this never fires.
_INFO_FILE_CHAR_CAP = 20_000


# ── Usage reporting ─────────────────────────────────────────────────────────
# TOKENS AND SEARCHES ONLY — no dollar or cent figure appears anywhere in
# Trellis (Ryan, 2026-09-07). The old _PRICING table, _WEB_SEARCH_RATE_CENTS,
# estimate_cost_cents() and all the cents arithmetic were deleted with that
# decision.
#
# Two reasons, and the second is the load-bearing one. It is BYOK, so
# Anthropic's own console is the authoritative bill and anything here is a
# second opinion at best. And a hardcoded price table rots silently: the
# deleted one carried Sonnet 5 at introductory $2/$10 "through 2026-08-31"
# with a comment instructing a human to update the row or under-report by
# 50% — nobody did, and it under-reported for a week before anyone noticed.
# A token count cannot go stale, because it is a measurement rather than a
# claim about someone else's price list.

# Measured from real Performer-page runs (2026-08-07): a 5-search pass billed
# 74,737 input / 1,492 output tokens. INPUT DOMINATES, for the reason given
# above, so usage tracks the search count almost linearly and the search count
# is the only variable worth modelling. ~15k input tokens per search is the
# observed slope (74.7k / 5).
_EST_TOKENS_PER_SEARCH = 15_000
_EST_OUTPUT_TOKENS = 1_500
_EST_MIN_SEARCHES = 1


def estimate_tokens(max_searches=None):
    """
    (low, high) total tokens for one research pass.

    Deliberately a RANGE, not a point: how many searches the model decides it
    needs is not knowable in advance, and a single number would be a false
    promise. Model-independent, unlike the cost estimate it replaces — a token
    count is a property of the work, not of who is billed for it.
    """
    hi = MAX_SEARCHES if max_searches is None else max_searches
    def one(searches):
        return searches * _EST_TOKENS_PER_SEARCH + _EST_OUTPUT_TOKENS
    return one(_EST_MIN_SEARCHES), one(hi)


def _usage_summary(usage):
    """
    Token and web-search counts off an Anthropic Messages API `usage` object.
    Returns None when there is no usage object at all, so the UI can tell
    "not measured" apart from "measured as zero".

    Cache reads and writes are still counted separately from plain input even
    though nothing here prices them. They are genuinely different quantities —
    a cached read is a tenth of the price of a fresh one — so anyone checking
    these against their Anthropic bill needs them apart, and folding them
    together would make a cached dialogue turn look ten times more expensive
    than it was.
    """
    if usage is None:
        return None

    input_tokens  = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_read    = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write   = getattr(usage, "cache_creation_input_tokens", 0) or 0
    stu           = getattr(usage, "server_tool_use", None)
    searches      = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0

    return {
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write,
        "web_search_requests": searches,
        "total_tokens": input_tokens + output_tokens,
    }

_SYSTEM = """You are the Trellis Research Assistant, an expert archivist of live \
concert recordings (ROIOs). You verify and correct a recording's metadata using the \
web-search tool.

Rules:
- THE RECORDING'S OWN INFO FILE AND TAGS ARE EVIDENCE, NOT NOISE. If they give a \
specific date or venue and you cannot corroborate it online, the correct output is \
"not corroborated" — say so in verify_items and propose nothing for that field. Never \
replace a specific existing value with a different, better-documented show. A rare \
show having a thin online footprint is expected and is not evidence against it, and a \
famous show that merely resembles this one is not this one. Propose replacing a \
specific existing value ONLY when you have evidence tying your source to THIS \
recording: a matching setlist, a matching taper or lineage, or an explicit reference \
to the same date at the same venue.
- Returning ZERO proposals is a good result when the current values already look \
right. Do not manufacture a change to seem useful.
- If the human attached a question, answer it in 'answer' — directly, in a few plain \
sentences, researching it if it needs research. A question is the human telling you \
where the effort is wanted, so weight it above the routine checks. If you cannot \
answer it, say so plainly rather than answering a different question.
- Suggest, never assert. Every proposal needs a confidence (high|medium|low), a \
source (web|info_file|tags|db_match), and a url when web-based.
- confidence "high" ONLY when corroborated by >=2 INDEPENDENT, authoritative sources. \
The recording's own tags, info file, and DB entry are NOT independent of each other \
(they usually share one origin) — treat them as a single source.
- Prefer canonical/authoritative sources (setlist.fm, artist official sites, \
institutional archives, etree) over forums.
- Propose scalar fields (artist, date, venue, city, state, country, source, lineage, \
event) in 'proposals'.
- Every discrepancy you describe in 'thinking' MUST also appear as a structured entry in \
'proposals' — never narrate a correction ("the date is actually...", "this was really \
recorded at...") without also emitting the matching proposal(s). If you're not fully \
certain, propose it anyway at medium or low confidence rather than only mentioning it in \
prose — low-confidence proposals are never auto-applied, so submitting one is always safe \
and puts the finding in front of the human either way. A narrative-only finding with no \
matching proposal is a bug, not a valid result. When you correct a date or venue, also \
check whether city/state/country need a matching correction (a wrong venue often means \
the location fields are wrong too) and propose those alongside it.
- ALWAYS actively research the track listing / setlist — both online (setlist.fm, \
archive.org/etree, official sources) AND in the info file text if one is provided — and \
return it in 'track_titles' as {number, title} for as many of the audio files as you can \
confidently identify. Track titles are the single most valuable field and are usually \
missing — this is a primary goal of the pass. Order them to match the audio files. If the \
audio file count does not match the setlist you find, still return your best-ordered \
titles and flag the count discrepancy in verify_items. If you CANNOT find a setlist \
anywhere, say so explicitly in verify_items ("No setlist found for this recording"). \
Never invent titles.
- The info file often contains a real setlist typed as plain, unnumbered lines — no \
"1.", no track numbers, just song titles one per line in the spot where a tracklist \
normally goes. Recognize that pattern and treat it as a primary source, not just prose to \
skim:
  * Segue notation appended to a title ("->", "-->", "/") marks a transition into the \
next song — strip it, it isn't part of the title.
  * Footnote markers appended to a title (*, **, ***, †, ^, or a bracketed number) point \
to an annotation elsewhere in the file (often personnel/lineup notes). Strip the marker \
from the title; the annotation text itself is worth keeping but belongs in \
provenance_notes, not in the title.
  * Section/break labels ("Set I", "Set II", "Encore", "Disc 1", "Intro") are structural \
headers, not songs — skip them, but they confirm you're reading the tracklist section.
  * A line that's only a time value (e.g. "3:40") is a duration, not a title — drop it.
  * Named improvisation/instrumental segments ("Drums", "Bass", "Jam", "Tuning") ARE \
legitimate track titles in live-show setlists — don't discard them just because they're \
short or generic-sounding.
  * Once you have a clean candidate list, cross-check it against your web research (the \
artist's actual setlist for that date, or their general repertoire if the exact show \
isn't findable) to raise your confidence and correct spelling/capitalization. Compare the \
candidate count to the number of audio files as a sanity check, but a mismatch alone is \
not a reason to withhold them — flag it in verify_items instead.
- Dates are ISO (YYYY-MM-DD, partial ok). Venue = the real physical venue \
(canonicalize nicknames). Location = city, state (US only), country.
- Split your notes: verify_items = things the human should double-check (transient); \
provenance_notes = lasting facts worth keeping (date disputes, broadcast/remaster \
context, source anomalies).
- When done, call submit_analysis exactly once with your findings. Put your reasoning \
narrative in 'thinking' — keep it to 2-4 concise sentences. Be economical: don't repeat \
the proposals in prose.
"""

_SUBMIT_TOOL = {
    "name": "submit_analysis",
    "description": "Submit your final research findings for human review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "thinking": {"type": "string", "description": "Your reasoning narrative."},
            "answer": {"type": "string",
                       "description": "Direct answer to the human's question, if one was asked."},
            "proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field":      {"type": "string", "enum": _PROPOSAL_FIELDS},
                        "current":    {"type": "string"},
                        "proposed":   {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "source":     {"type": "string",
                                       "enum": ["web", "info_file", "tags", "db_match"]},
                        "url":        {"type": "string"},
                    },
                    "required": ["field", "proposed", "confidence", "source"],
                },
            },
            "track_titles": {
                "type": "array",
                "description": "Researched setlist, ordered to match the audio files.",
                "items": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer"},
                        "title":  {"type": "string"},
                    },
                    "required": ["number", "title"],
                },
            },
            "verify_items":     {"type": "array", "items": {"type": "string"}},
            "provenance_notes": {"type": "array", "items": {"type": "string"}},
            "sources": {
                "type": "array",
                "items": {"type": "object",
                          "properties": {"title": {"type": "string"}, "url": {"type": "string"}}},
            },
        },
        "required": ["thinking", "proposals"],
    },
}


def _metadata_summary(current, folder_name):
    """Human-readable dump of the current metadata for the prompt."""
    lines = ["Folder name: %s" % folder_name, "", "Current metadata (to verify/correct):"]
    for k in ("artist", "date", "venue", "city", "state", "country", "source", "lineage", "event"):
        v = current.get(k)
        if v:
            lines.append("  %s: %s" % (k, v))
    tracks = current.get("tracks") or []
    if tracks:
        lines.append("")
        lines.append("Tracks on disk (%d) — titles may be missing/uncertain:" % len(tracks))
        for t in tracks[:60]:
            dur = t.get("duration")
            dur_s = " [%d:%02d]" % (int(dur) // 60, int(dur) % 60) if dur else ""
            lines.append("  %s. %s%s" % (t.get("number", "?"), t.get("title") or "(untitled)", dur_s))
    return "\n".join(lines)


def _prior_summary(prior):
    """
    Compact recap of a previous run on this same recording, for a re-run.

    Re-running used to start from absolute zero: the saved ai_research_json was
    never fed back, so the model re-searched everything it had already found and
    was free to contradict itself. That is not hypothetical — the incident that
    triggered this whole spec was two runs on one folder confidently returning
    two DIFFERENT wrong dates (Danny Gatton, Cellar Door, 2026-07-20).

    Roughly 200 tokens against the ~15,000 a single avoided search costs, so it
    pays for itself the first time it saves one.
    """
    if not prior:
        return ""
    lines = []
    for pr in (prior.get("proposals") or [])[:12]:
        lines.append("  %s -> %s (%s)" % (pr.get("field"), pr.get("proposed"),
                                          pr.get("confidence") or "?"))
    for v in (prior.get("verify_items") or [])[:6]:
        lines.append("  unresolved: %s" % v)
    if not lines:
        return ""
    return ("A PREVIOUS RUN of yours on this same recording proposed the following. "
            "That was YOU, not a second source — it corroborates nothing, and a human "
            "may have already rejected some of it. Use it to avoid repeating searches "
            "you have already done. If you now disagree with any of it, say so "
            "explicitly in 'thinking' and propose your new value; silently returning a "
            "different answer than last time is the failure this recap exists to "
            "prevent.\n" + "\n".join(lines) + "\n")


def _extract_analysis(resp):
    """Pull the submit_analysis tool input from the response; fall back to text."""
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_analysis":
            return dict(block.input)
    # Fallback: model returned prose instead of calling the tool.
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
    return {"thinking": text.strip() or "No structured analysis returned.",
            "proposals": [], "verify_items": [], "provenance_notes": [], "sources": []}


def run_ai_assist(folder_path, current, api_key, model, *, question=None, prior=None):
    """
    Run the research pass. Returns a dict:
      {thinking, answer, proposals:[...], track_titles:[...], verify_items:[...],
       provenance_notes:[...], sources:[...], usage, model}

    `question` is the human's optional pre-run question; `prior` is the saved
    result of an earlier run on this same recording, if there is one.
    Raises AiAssistError on any recoverable failure.
    """
    if not _HAS_SDK:
        raise AiAssistError("The 'anthropic' package is not installed. Run: pip install anthropic")
    if not api_key:
        raise AiAssistError("No Anthropic API key configured.")

    content = [
        {"type": "text", "text": _metadata_summary(current, os.path.basename(folder_path))},
    ]
    recap = _prior_summary(prior)
    if recap:
        content.append({"type": "text", "text": recap})

    info_text = (current.get("info_file_content") or "").strip()
    if info_text:
        truncated = len(info_text) > _INFO_FILE_CHAR_CAP
        if truncated:
            info_text = info_text[:_INFO_FILE_CHAR_CAP]
        content.append({"type": "text", "text":
            "Info file contents, as found in the recording's folder (may contain an "
            "unnumbered setlist — see the info-file parsing rules above):\n---\n"
            + info_text
            + ("\n[TRUNCATED — this info file was too long to send in full. If the "
               "setlist appears to be cut off, say so in verify_items rather than "
               "guessing the rest.]" if truncated else "")
            + "\n---"})

    question = (question or "").strip()
    if question:
        content.append({"type": "text", "text": "The archivist asks: %s" % question})
    content.append({"type": "text",
                     "text": "Research this recording and call submit_analysis with your findings."})

    # A question earns the bigger search budget: it is the human saying where the
    # effort is wanted, which is a better signal than spending it by default.
    max_uses = MAX_SEARCHES_WITH_QUESTION if question else MAX_SEARCHES

    _log("start folder=%r model=%s q=%s prior=%s max_uses=%d"
         % (os.path.basename(folder_path), model, bool(question), bool(recap), max_uses))

    client = anthropic.Anthropic(api_key=api_key, timeout=600.0)
    t0 = time.time()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            # cache_control on the last system block caches everything above it.
            # The system prompt is static and byte-identical on every run, so
            # after the first pass inside the cache window it bills at a tenth
            # of the input rate. A modest win today (~1.4k tokens); it is here
            # NOW because any future multi-turn feature is unaffordable without
            # it, and retrofitting caching to a shipped dialogue is worse than
            # having it already in place.
            system=[{"type": "text", "text": _SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            tools=[
                {"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses},
                _SUBMIT_TOOL,
            ],
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError:
        raise AiAssistError("Anthropic rejected the API key (check it in Settings).")
    except anthropic.APITimeoutError:
        raise AiAssistError("Anthropic request timed out (research took too long).")
    except anthropic.APIError as e:
        _log("APIError: %s" % getattr(e, "message", str(e)))
        raise AiAssistError("Anthropic API error: %s" % getattr(e, "message", str(e)))

    _log("anthropic returned in %.1fs stop_reason=%s" % (time.time() - t0, getattr(resp, "stop_reason", None)))
    result = _extract_analysis(resp)
    _log("proposals=%d verify=%d provenance=%d"
         % (len(result.get("proposals", [])), len(result.get("verify_items", [])),
            len(result.get("provenance_notes", []))))
    # Normalize: ensure list keys exist and drop any non-scalar proposals defensively.
    result.setdefault("track_titles", [])
    result.setdefault("verify_items", [])
    result.setdefault("provenance_notes", [])
    result.setdefault("sources", [])
    result["proposals"] = [p for p in result.get("proposals", [])
                           if p.get("field") in _PROPOSAL_FIELDS and p.get("proposed")]
    result["model"] = model

    usage = _usage_summary(getattr(resp, "usage", None))
    result["usage"] = usage
    if usage:
        _log("usage: in=%d out=%d searches=%d"
             % (usage["input_tokens"], usage["output_tokens"],
                usage["web_search_requests"]))
    return result
