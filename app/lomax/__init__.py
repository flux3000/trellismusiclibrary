"""
Lomax, the research assistant. Public API; nothing outside this package reaches into it.

    start_run()      create a run row and start its worker (a thread; inline when the app is
                     configured LOMAX_INLINE, as in tests)
    run_now()        create a run and finish it before returning
    get_run(id)      a run as a dict, proposals included
    latest_run()     the latest done run for a skill and subject
    latest_result()  the raw result of the latest done run among several skills, in the payload
                     shape the pages already read
    decide()         accept or reject one proposal (accepting applies it)
    estimate()       token range for a skill and level
    repoint_folder() move a folder run to the recording its import created
    SKILLS           the registry
"""
import json
import os
import re
from datetime import datetime, timezone

from flask import current_app

from app.extensions import db
from app.lomax import core
from app.lomax.apply import ApplyError, apply_proposal
from app.lomax.core import LomaxConflict, LomaxError, LomaxNotFound, norm_key
from app.lomax.skills import SKILLS
from app.models.lomax import LomaxProposal, LomaxRun

__all__ = ["SKILLS", "LomaxError", "LomaxNotFound", "ApplyError", "create_run", "start_run", "run_now",
           "get_run", "list_runs", "latest_run", "latest_result", "decide", "estimate", "repoint_folder",
           "adopt_result", "run_dict", "restore", "usage_log", "LomaxConflict"]

_SUBJECT_MODELS = {"recording": "Recording", "artist": "Artist", "venue": "Venue"}


def _parsed(text):
    try:
        return json.loads(text) if text else None
    except (ValueError, TypeError):
        return None


def _iso(dt):
    return dt.isoformat() if dt else None


def proposal_dict(p):
    return {"id": p.id, "run_id": p.run_id, "field": p.field, "current": p.current, "proposed": p.proposed,
            "confidence": p.confidence, "source": p.source, "url": p.url, "decision": p.decision,
            "challenge": bool(p.challenge), "agrees": bool(p.agrees),
            "decided_at": _iso(p.decided_at)}


from app.lomax import filters  # noqa: E402

_NOTE_FIELD = re.compile(r"^track\.(\d+)\.note$")


def _info_text(run):
    """The info file text the run's subject carries: what a folder run was sent, or the recording's own."""
    if run.subject_type == "folder":
        return (_parsed(run.input_json) or {}).get("info_file_content") or ""
    if run.subject_type == "recording" and run.subject_id:
        from app.models import Recording
        rec = db.session.get(Recording, run.subject_id)
        return (rec.info_file_content or "") if rec else ""
    return ""


def clean_result(run, result):
    """The result with every track note the info file does not support removed, and every lineage cut to its chain, on a copy of what was
    stored (runs made before the filter, or by an older rule, are cleaned as they are served). The stored
    row is never changed. Only recording-skill results carry track notes."""
    if not isinstance(result, dict) or run.skill not in ("recording", "resolution"):
        return result
    tracks, props = result.get("tracks"), result.get("proposals")
    if not (tracks or props):
        return result
    info = _info_text(run)
    titles = {t.get("number"): t.get("title") for t in (tracks or []) if isinstance(t, dict)}
    for t in (_parsed(run.input_json) or {}).get("tracks") or [] if run.subject_type == "folder" else []:
        if isinstance(t, dict):
            titles.setdefault(t.get("number"), t.get("title"))

    def keep(number, note):
        return filters.filter_track_note(note, titles.get(number) or "", info, None, number)

    out = dict(result)
    if tracks:
        out["tracks"] = [dict(t, note=keep(t.get("number"), t.get("note"))) if isinstance(t, dict) and t.get("note") else t
                         for t in tracks]
    if props:
        kept = []
        for p in props:
            m = _NOTE_FIELD.match(str(p.get("field") or "")) if isinstance(p, dict) else None
            if m and not keep(int(m.group(1)), p.get("proposed")):
                continue
            if isinstance(p, dict) and p.get("field") == "lineage":
                chain = filters.clean_lineage(p.get("proposed"))
                if not chain:
                    continue
                p = dict(p, proposed=chain, agrees=filters.same_text(chain, p.get("current")))
            kept.append(p)
        out["proposals"] = kept
    return out


def run_dict(run):
    result = _parsed(run.result_json)
    skill = "recording" if run.skill == "resolution" else run.skill      # legacy rows still read
    # A folder run keeps the identity of the show it was made for (audio files plus info file) inside
    # the data the page sent, so a path reused for different content is not mistaken for the same show.
    fingerprint = (_parsed(run.input_json) or {}).get("fingerprint") if run.subject_type == "folder" else None
    if result is not None and run.proposals:
        result["proposals"] = [proposal_dict(p) for p in run.proposals]
    result = clean_result(run, result)
    return {"id": run.id, "skill": skill, "subject_type": run.subject_type, "subject_id": run.subject_id,
            "subject_key": run.subject_key, "level": core.LEVEL_ALIASES.get(run.level, run.level), "question": run.question,
            "status": run.status, "result": result, "usage": _parsed(run.usage_json), "model": run.model,
            "error": run.error, "created_by": run.created_by, "fingerprint": fingerprint,
            "created_at": _iso(run.created_at), "finished_at": _iso(run.finished_at)}


def create_run(skill, subject_type, *, subject_id=None, subject_key=None, level="research",
               question=None, current=None, user_id=None):
    """Validate and insert a queued run. Raises LomaxError (LomaxNotFound for a missing subject)."""
    skill = "recording" if skill == "resolution" else skill      # the two skills merged
    level = core.LEVEL_ALIASES.get(level, level)
    sk = SKILLS.get(skill)
    if sk is None:
        raise LomaxError("Unknown skill %r." % skill)
    if subject_type not in sk.subject_types:
        raise LomaxError("The %s skill does not work on a %s." % (sk.label or skill, subject_type))
    if level not in core.LEVELS:
        raise LomaxError("Level must be study or research.")
    if subject_type == "folder":
        subject_key = norm_key((subject_key or "").strip())
        if not subject_key:
            raise LomaxError("A folder run needs subject_key.")
        subject_id = None
    else:
        from app import models
        model = getattr(models, _SUBJECT_MODELS[subject_type])
        if not subject_id or db.session.get(model, subject_id) is None:
            raise LomaxNotFound("%s not found." % subject_type.capitalize())
        subject_key = None
    run = LomaxRun(skill=skill, subject_type=subject_type, subject_id=subject_id, subject_key=subject_key,
                   level=level, question=(question or "").strip() or None, status="queued",
                   model=core.default_model(user_id), created_by=user_id,
                   input_json=json.dumps(current) if (subject_type == "folder" and current) else None)
    db.session.add(run)
    db.session.commit()
    return run


def start_run(skill, subject_type, *, api_key, **kw):
    run = create_run(skill, subject_type, **kw)
    core.spawn(current_app._get_current_object(), run.id, api_key)
    return db.session.get(LomaxRun, run.id)


def run_now(skill, subject_type, *, api_key, **kw):
    run = create_run(skill, subject_type, **kw)
    core.execute(run.id, api_key)
    return db.session.get(LomaxRun, run.id)


def get_run(run_id):
    run = db.session.get(LomaxRun, run_id)
    return run_dict(run) if run else None


def list_runs(skill, subject_type, subject_id=None, subject_key=None, limit=50):
    """Every run on one subject, oldest first, whatever its status: the chat history, the
    Questions list, and the way a page finds a run still working when it was left."""
    skills = ("recording", "resolution") if skill in ("recording", "resolution") else (skill,)
    q = db.session.query(LomaxRun).filter(LomaxRun.subject_type == subject_type, LomaxRun.skill.in_(list(skills)))
    if subject_type == "folder":
        q = q.filter(LomaxRun.subject_key == norm_key((subject_key or "").strip()))
    else:
        q = q.filter(LomaxRun.subject_id == subject_id)
    rows = q.order_by(LomaxRun.id.desc()).limit(max(1, min(int(limit), 200))).all()
    return [run_dict(r) for r in reversed(rows)]


def _latest_done(subject_type, subject_id, skills):
    return (db.session.query(LomaxRun)
            .filter(LomaxRun.subject_type == subject_type, LomaxRun.subject_id == subject_id,
                    LomaxRun.skill.in_(list(skills)), LomaxRun.status == "done")
            .order_by(LomaxRun.id.desc()).first())


def latest_run(skill, subject_type, subject_id):
    skills = ("recording", "resolution") if skill in ("recording", "resolution") else (skill,)
    run = _latest_done(subject_type, subject_id, skills)
    return run_dict(run) if run else None


def latest_result(subject_type, subject_id, skills, *, not_mode=None):
    """The stored result of the latest done run among `skills`, untouched, or None. `not_mode`
    skips migrated results of that legacy mode ("bio" or "lineup"): an artist's two old columns
    became two runs, and each page payload wants the newest one that is not the other kind."""
    runs = (db.session.query(LomaxRun)
            .filter(LomaxRun.subject_type == subject_type, LomaxRun.subject_id == subject_id,
                    LomaxRun.skill.in_(list(skills)), LomaxRun.status == "done")
            .order_by(LomaxRun.id.desc()).limit(10))
    for run in runs:
        result = _parsed(run.result_json)
        if isinstance(result, dict) and (not_mode is None or result.get("mode") != not_mode):
            return clean_result(run, result)
    return None


def decide(proposal_id, decision, user_id=None):
    """Record accept or reject. Accepting applies the value through the field's normal save path
    (not again if it was already accepted); a failed apply leaves the decision unrecorded."""
    if decision not in ("accepted", "rejected"):
        raise LomaxError("decision must be accepted or rejected.")
    prop = db.session.get(LomaxProposal, proposal_id)
    if prop is None:
        raise LomaxNotFound("Proposal not found.")
    if prop.agrees:
        raise LomaxError("Lomax agrees with this value; there is nothing to decide.")
    if decision == "accepted" and prop.decision != "accepted":
        apply_proposal(prop, prop.run, user_id)
    prop.decision, prop.decided_at = decision, datetime.now(timezone.utc)
    db.session.commit()
    return proposal_dict(prop)


def estimate(skill, level="research", question=False):
    skill = "recording" if skill == "resolution" else skill
    if skill not in SKILLS:
        raise LomaxError("Unknown skill %r." % skill)
    level = core.LEVEL_ALIASES.get(level, level)
    return core.estimate_tokens(skill, level if level in core.LEVELS else "research", question)


def repoint_folder(folder_path, recording_id):
    """The import saved this folder as a recording: its runs now belong to the recording.
    Returns how many runs moved."""
    key = norm_key(folder_path)
    if not key:
        return 0
    n = (db.session.query(LomaxRun)
         .filter(LomaxRun.subject_type == "folder", LomaxRun.subject_key == key)
         .update({"subject_type": "recording", "subject_id": recording_id, "subject_key": None,
                  "input_json": None}, synchronize_session=False))
    return n


def adopt_result(recording_id, result, user_id=None, skill=None):
    """File a result the page already holds (Add Recording before the recording existed) as a done
    run on the recording. Not committed; the caller's save owns the transaction."""
    skill = skill or (result or {}).get("skill") or "recording"      # an album result says so itself
    if skill not in ("recording", "album"):
        skill = "recording"
    run = LomaxRun(skill=skill, subject_type="recording", subject_id=recording_id, level="research",
                   status="done", model=(result or {}).get("model"), created_by=user_id,
                   result_json=json.dumps(result), usage_json=json.dumps((result or {}).get("usage")),
                   finished_at=datetime.now(timezone.utc), migrated_from="client")
    db.session.add(run)
    core._store_proposals(run, [p for p in (result or {}).get("proposals") or [] if isinstance(p, dict)])
    return run


_RESTORABLE = {"bio": ("artist", "bio"), "history": ("venue", "history")}


def restore(run_id):
    """Put back the text an auto-apply replaced, only if the field still holds what the run wrote.
    Raises LomaxConflict('edited_since') otherwise. Records restored_at in the result."""
    from app import models
    run = db.session.get(LomaxRun, run_id)
    result = _parsed(run.result_json) if run else None
    rep = (result or {}).get("replaced")
    if not rep:
        raise LomaxNotFound("Nothing to restore.")
    if result.get("restored_at"):
        raise LomaxError("Already restored.")
    subject_type, attr = _RESTORABLE[rep["field"]]
    obj = db.session.get(getattr(models, subject_type.capitalize()), run.subject_id)
    if obj is None:
        raise LomaxNotFound("The %s no longer exists." % subject_type)
    if (getattr(obj, attr) or "") != rep["written"]:
        raise LomaxConflict("edited_since")
    setattr(obj, attr, rep["text"] or None)
    result["restored_at"] = datetime.now(timezone.utc).isoformat()
    run.result_json = json.dumps(result)
    db.session.commit()
    return run_dict(run)


SKILL_LABELS = {"resolution": "Recording", "recording": "Recording",
                "artist": "Artist History", "venue": "Venue History", "album": "Album"}


def _subject_label(run):
    from app import models
    if run.subject_type == "folder":
        return os.path.basename((run.subject_key or "").rstrip("/\\"))
    if run.subject_type == "recording":
        rec = db.session.get(models.Recording, run.subject_id)
        p = rec.performance if rec else None
        if not p:
            return ""
        from app.utils.format import format_partial_date
        return " \u00b7 ".join(x for x in (p.artist.name if p.artist else "",
                                           format_partial_date(p.start_year, p.start_month, p.start_day) or "",
                                           p.venue.name if p.venue else "") if x)
    obj = db.session.get(getattr(models, run.subject_type.capitalize()), run.subject_id)
    return obj.name if obj else ""


def usage_log(limit=50, offset=0):
    """Install-wide tokens and searches from lomax_run.usage_json, plus a page of runs, newest
    first. Tokens and searches only; no currency."""
    from sqlalchemy import func
    limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
    rows = db.session.query(LomaxRun.usage_json).all()
    tot = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "web_searches": 0}
    for (blob,) in rows:
        u = _parsed(blob) or {}
        tot["input_tokens"] += u.get("input_tokens") or 0
        tot["output_tokens"] += u.get("output_tokens") or 0
        tot["total_tokens"] += u.get("total_tokens") or 0
        tot["web_searches"] += u.get("web_search_requests") or 0
    # Suggestions only: a value Lomax agrees with is not a suggestion.
    counts = dict(db.session.query(LomaxProposal.run_id, func.count(LomaxProposal.id))
                  .filter(LomaxProposal.agrees.is_(False)).group_by(LomaxProposal.run_id).all())
    acc = dict(db.session.query(LomaxProposal.run_id, func.count(LomaxProposal.id))
               .filter(LomaxProposal.decision == "accepted").group_by(LomaxProposal.run_id).all())
    since = db.session.query(func.min(LomaxRun.created_at)).scalar()
    page = (db.session.query(LomaxRun).order_by(LomaxRun.id.desc()).offset(offset).limit(limit).all())
    runs = []
    for r in page:
        u = _parsed(r.usage_json) or {}
        runs.append({"id": r.id, "created_at": _iso(r.created_at),
                     "skill": "recording" if r.skill == "resolution" else r.skill,
                     "skill_label": SKILL_LABELS.get(r.skill, r.skill), "subject_type": r.subject_type,
                     "subject_id": r.subject_id, "subject_label": _subject_label(r),
                     "level": core.LEVEL_ALIASES.get(r.level, r.level),
                     "status": r.status, "error": r.error, "question": r.question,
                     "proposals": counts.get(r.id, 0), "accepted": acc.get(r.id, 0),
                     "total_tokens": u.get("total_tokens") or 0, "web_searches": u.get("web_search_requests") or 0})
    return {"totals": dict(tot, runs=len(rows), proposals=sum(counts.values()), accepted=sum(acc.values()),
                           since=_iso(since)),
            "runs": runs, "total": len(rows)}
