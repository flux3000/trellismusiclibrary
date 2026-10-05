"""
Lomax core: the one Anthropic call, the usage summary, result extraction, prose cleaning,
and the run worker. Nothing here knows about a particular skill.

Usage is TOKENS AND SEARCHES ONLY. No dollar or cent figure appears anywhere in Trellis
(Ryan, 2026-09-07): it is BYOK, Anthropic's console is the authoritative bill, and a
hardcoded price table rots silently. A token count is a measurement, not a claim about
someone else's price list.
"""
import json
import logging
import os
import re
import threading
import time
import traceback
from datetime import datetime, timezone

from app.extensions import db
from app.lomax import prompts
from app.lomax.skills import SKILLS
from app.lomax.skills.base import Ctx, NothingToDo, Subject
from app.models.lomax import LomaxProposal, LomaxRun

try:   # guarded so the module loads when the SDK is not installed yet
    import anthropic
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False

log = logging.getLogger("trellis.lomax")

MODEL_FALLBACK = "claude-sonnet-5"
LEVELS = ("study", "research")
LEVEL_ALIASES = {"read": "study"}     # accepted on input for one release

# Search is ~95% of the token bill (~15k input tokens per search, measured) and worse than
# linear: every result stays in the context for every later turn of the same run. 5 is the
# default; a run carrying a question from the archivist gets 7, because a question is
# evidence that the extra digging is wanted (Ryan, 2026-09-07).
MAX_SEARCHES = 5
MAX_SEARCHES_WITH_QUESTION = 7
_EST_TOKENS_PER_SEARCH = 15_000
_EST_OUTPUT_TOKENS = 1_500

_PROSE_KEYS = ("biography", "history", "thinking", "answer")


class LomaxError(Exception):
    """A recoverable failure in a run; the message is shown to the archivist."""


class LomaxConflict(LomaxError):
    """The state moved on since the run (a restore after a manual edit)."""


class LomaxNotFound(LomaxError):
    """The subject (or run, or proposal) does not exist."""


def _say(msg):
    print("[lomax] " + msg, flush=True)


def _now():
    return datetime.now(timezone.utc)


def default_model(user_id=None):
    """The one model default: preference `ai_model`, else the constant above."""
    from app.utils.prefs import get_pref
    return (get_pref(user_id, "ai_model") if user_id else None) or MODEL_FALLBACK


def search_budget(question):
    return MAX_SEARCHES_WITH_QUESTION if (question or "").strip() else MAX_SEARCHES


# ── usage and estimates ──────────────────────────────────────────────────────

def usage_summary(usage):
    """Token and web-search counts off a Messages API `usage` object; None when there is no usage
    object, so the page can tell "not measured" from "measured as zero". Cache reads and writes
    stay separate from plain input: a cached read is a tenth of the price of a fresh one."""
    if usage is None:
        return None
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    stu = getattr(usage, "server_tool_use", None)
    return {
        "input_tokens": inp, "output_tokens": out,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "web_search_requests": (getattr(stu, "web_search_requests", 0) or 0) if stu else 0,
        "total_tokens": inp + out,
    }


def estimate_tokens(skill_key, level="research", question=False):
    """{low_tokens, high_tokens, max_searches} for one run. A RANGE: how many searches the model
    decides it needs is not knowable in advance, and one number would be a false promise."""
    sk = SKILLS[skill_key]
    if level in ("read", "study"):
        return {"low_tokens": sk.base_tokens + _EST_OUTPUT_TOKENS,
                "high_tokens": sk.base_tokens * 2 + _EST_OUTPUT_TOKENS, "max_searches": 0}
    hi = MAX_SEARCHES_WITH_QUESTION if question else MAX_SEARCHES
    return {"low_tokens": sk.base_tokens + _EST_TOKENS_PER_SEARCH + _EST_OUTPUT_TOKENS,
            "high_tokens": sk.base_tokens + hi * _EST_TOKENS_PER_SEARCH + _EST_OUTPUT_TOKENS,
            "max_searches": hi}


# ── prose and extraction ─────────────────────────────────────────────────────

# Citation markup the web-search tool leaves in generated prose ("<cite index=...>", "[1]").
# Stripped server-side so what is stored is already clean (2026-08-07).
_CITE_RE = re.compile(r"</?cite[^>]*>", re.IGNORECASE)
_REF_RE = re.compile(r"\[\s*\d+(?:\s*[,–-]\s*\d+)*\s*\]")
_WS_RE = re.compile(r"[ \t]{2,}")


def clean_prose(text):
    """Strip citation markup and tidy whitespace, preserving paragraph breaks."""
    if not text:
        return text
    out = _REF_RE.sub("", _CITE_RE.sub("", text))
    out = re.sub(r"\s+([.,;:!?])", r"\1", out)
    out = _WS_RE.sub(" ", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def extract_result(resp, tool_name):
    """The submit tool's input, prose-cleaned; the model's text when it did not call the tool."""
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            data = dict(block.input)
            for k in _PROSE_KEYS:
                if isinstance(data.get(k), str):
                    data[k] = clean_prose(data[k])
            return data
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
    return {"thinking": clean_prose(text) or "No structured result returned.", "proposals": []}


# ── the call ─────────────────────────────────────────────────────────────────

def call_model(skill, user_text, *, level, question, api_key, model):
    """One Messages API call. Returns (raw tool input, usage summary)."""
    if not _HAS_SDK:
        raise LomaxError("The 'anthropic' package is not installed. Run: pip install anthropic")
    if not api_key:
        raise LomaxError("No Anthropic API key configured.")
    tool_name = skill.submit_tool["name"]
    tools = [skill.submit_tool]
    kwargs = {}
    if level == "research":
        tools.insert(0, {"type": "web_search_20250305", "name": "web_search",
                         "max_uses": search_budget(question)})
    else:
        kwargs["tool_choice"] = {"type": "tool", "name": tool_name}   # Read sends no web tool
    # cache_control on a block caches everything above it. BASE is byte-identical across every
    # skill, so it is cached once for all of them; the skill's section is cached per skill.
    system = [{"type": "text", "text": prompts.BASE, "cache_control": {"type": "ephemeral"}},
              {"type": "text", "text": skill.instructions, "cache_control": {"type": "ephemeral"}}]
    client = anthropic.Anthropic(api_key=api_key, timeout=600.0)
    t0 = time.time()
    try:
        resp = client.messages.create(model=model, max_tokens=8000, system=system, tools=tools,
                                      messages=[{"role": "user", "content": user_text}], **kwargs)
    except anthropic.AuthenticationError:
        raise LomaxError("Anthropic rejected the API key (check it in Settings).")
    except anthropic.APITimeoutError:
        raise LomaxError("Anthropic request timed out (research took too long).")
    except anthropic.APIError as e:
        _say("APIError: %s" % getattr(e, "message", str(e)))
        raise LomaxError("Anthropic API error: %s" % getattr(e, "message", str(e)))
    _say("%s returned in %.1fs stop_reason=%s" % (skill.key, time.time() - t0, getattr(resp, "stop_reason", None)))
    return extract_result(resp, tool_name), usage_summary(getattr(resp, "usage", None))


# ── subjects and recaps ──────────────────────────────────────────────────────

def norm_key(path):
    return os.path.normpath(path) if path else path


def load_subject(run):
    from app.models.artist import Artist
    from app.models.recording import Recording
    from app.models.venue import Venue
    from app.lomax.evidence import current_from_recording
    t = run.subject_type
    if t == "folder":
        try:
            current = json.loads(run.input_json) if run.input_json else {}
        except (ValueError, TypeError):
            current = {}
        return Subject("folder", key=run.subject_key, current=current)
    model = {"recording": Recording, "artist": Artist, "venue": Venue}[t]
    obj = db.session.get(model, run.subject_id)
    if obj is None:
        raise LomaxError("The %s no longer exists." % t)
    return Subject(t, id=obj.id, obj=obj,
                   current=current_from_recording(obj) if t == "recording" else {})


def _same_subject(q, run):
    q = q.filter(LomaxRun.skill == run.skill, LomaxRun.subject_type == run.subject_type)
    if run.subject_type == "folder":
        return q.filter(LomaxRun.subject_key == run.subject_key)
    return q.filter(LomaxRun.subject_id == run.subject_id)


def _proposal_dict(p):
    return {"field": p.field, "proposed": p.proposed, "confidence": p.confidence, "decision": p.decision}


def prior_for(run):
    """What earlier done runs on this subject and skill said, for the recap: the latest run's
    proposals with their decisions, its open questions, and every rejection from any earlier run."""
    runs = (_same_subject(db.session.query(LomaxRun), run)
            .filter(LomaxRun.status == "done", LomaxRun.id != run.id)
            .order_by(LomaxRun.id.desc()).limit(10).all())
    if not runs:
        return None
    try:
        result = json.loads(runs[0].result_json or "{}")
    except (ValueError, TypeError):
        result = {}
    props = [_proposal_dict(p) for p in runs[0].proposals if not p.agrees]
    if not props:   # a migrated legacy run keeps its proposals in the result only
        props = [{"field": p.get("field"), "proposed": p.get("proposed"),
                  "confidence": p.get("confidence"), "decision": None}
                 for p in (result.get("proposals") or []) if isinstance(p, dict) and not p.get("agrees")]
    rejected = [_proposal_dict(p) for r in runs for p in r.proposals if p.decision == "rejected"]
    return {"proposals": props, "verify_items": result.get("verify_items") or [], "rejected": rejected}


# ── running ──────────────────────────────────────────────────────────────────

def build_user_text(skill, subject, run, ctx):
    sections = skill.gather(subject, ctx)
    parts = [prompts.render_sections(sections), prompts.recap_text(prior_for(run))]
    if ctx.question:
        parts.append("The archivist asks: %s" % ctx.question)
    parts.append(prompts.level_text(ctx.level, ctx.max_searches))
    parts.append("Call %s with your findings." % skill.submit_tool["name"])
    return "\n\n".join(p for p in parts if p)


def _store_proposals(run, proposals):
    for p in proposals:
        proposed = p.get("proposed")
        if proposed in (None, ""):
            continue
        run.proposals.append(LomaxProposal(
            field=p.get("field"), current=(None if p.get("current") in (None, "") else str(p["current"])),
            proposed=proposed if isinstance(proposed, str) else json.dumps(proposed),
            confidence=p.get("confidence"), source=p.get("source"), url=p.get("url") or None,
            challenge=bool(p.get("challenge")), agrees=bool(p.get("agrees"))))


def execute(run_id, api_key):
    """Do one run to completion inside the current app context. Never raises: a failure becomes
    status 'error' on the row."""
    run = db.session.get(LomaxRun, run_id)
    if run is None:
        return
    run.status, run.error = "running", None
    db.session.commit()
    t0 = time.time()
    try:
        skill = SKILLS[run.skill]
        subject = load_subject(run)
        ctx = Ctx(level=run.level, question=(run.question or "").strip(),
                  max_searches=search_budget(run.question), user_id=run.created_by)
        usage = None
        try:
            text = build_user_text(skill, subject, run, ctx)
            raw, usage = call_model(skill, text, level=run.level, question=ctx.question,
                                    api_key=api_key, model=run.model)
        except NothingToDo as e:
            raw = {"thinking": str(e), "proposals": []}
        result, proposals = skill.normalize(raw, subject)
        result["model"], result["usage"], result["level"] = run.model, usage, run.level
        _store_proposals(run, proposals)
        run.result_json = json.dumps(result)
        run.usage_json = json.dumps(usage) if usage else None
        run.status, run.finished_at = "done", _now()
        db.session.commit()
        if skill.apply_auto:
            try:   # the finished run stands even when the auto-apply fails
                applied = skill.apply_auto(subject, result)
                if applied:
                    if "replaced" in applied:
                        result["replaced"] = applied.pop("replaced")
                    result["applied"] = applied
                    run.result_json = json.dumps(result)
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                traceback.print_exc()
        _say("run %d (%s) done in %.1fs" % (run.id, run.skill, time.time() - t0))
    except Exception as e:  # noqa: BLE001 - the row is the job; the worker must not die silently
        db.session.rollback()
        if not isinstance(e, LomaxError):
            traceback.print_exc()
        run = db.session.get(LomaxRun, run_id)
        run.status = "error"
        run.error = str(e) if isinstance(e, LomaxError) else "Unexpected error: %s" % e
        run.finished_at = _now()
        db.session.commit()


def spawn(app, run_id, api_key):
    """Run in a background thread, or inline when app.config['LOMAX_INLINE'] is set (tests)."""
    def work():
        with app.app_context():
            try:
                execute(run_id, api_key)
            finally:
                db.session.remove()
    if app.config.get("LOMAX_INLINE"):
        execute(run_id, api_key)
        return
    threading.Thread(target=work, daemon=True, name="lomax-%d" % run_id).start()
