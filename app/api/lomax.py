"""
/api/lomax -- the one blueprint for Lomax runs.

    POST /runs                {skill, subject_type, subject_id | subject_key, level, question, current?}
    GET  /runs/<id>
    GET  /latest?skill=&subject_type=&subject_id=
    POST /proposals/<id>      {decision: "accepted" | "rejected"}
    GET  /estimate?skill=&level=

`current` is only for folder subjects (unsaved import rows), where the database has nothing yet.
"""
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

import app.lomax as lomax
from app.utils.prefs import get_api_key

bp = Blueprint("lomax", __name__)


def _err(e):
    code = 404 if isinstance(e, lomax.LomaxNotFound) else 409 if isinstance(e, lomax.LomaxConflict) else 400
    return jsonify({"error": str(e)}), code


@bp.route("/runs", methods=["POST"])
@login_required
def create_run():
    data = request.get_json(silent=True) or {}
    api_key = get_api_key(current_user.id)
    if not api_key:
        return jsonify({"error": "no_api_key"}), 428      # the page sends the person to Settings
    try:
        run = lomax.start_run(
            data.get("skill"), data.get("subject_type"), api_key=api_key,
            subject_id=data.get("subject_id"), subject_key=data.get("subject_key"),
            level=data.get("level") or "research", question=data.get("question"),
            current=data.get("current"), user_id=current_user.id)
    except lomax.LomaxError as e:
        return _err(e)
    return jsonify(lomax.run_dict(run)), 202


@bp.route("/runs", methods=["GET"])
@login_required
def list_runs():
    skill, stype = request.args.get("skill"), request.args.get("subject_type")
    sid, key = request.args.get("subject_id", type=int), request.args.get("subject_key")
    skill = "recording" if skill == "resolution" else skill
    if skill not in lomax.SKILLS or not stype or not (sid or key):
        return jsonify({"error": "skill, subject_type and subject_id (or subject_key) are required"}), 400
    return jsonify({"runs": lomax.list_runs(skill, stype, sid, key, request.args.get("limit", 50, type=int))})


@bp.route("/runs/<int:run_id>", methods=["GET"])
@login_required
def get_run(run_id):
    run = lomax.get_run(run_id)
    if run is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(run)


@bp.route("/runs/<int:run_id>/restore", methods=["POST"])
@login_required
def restore(run_id):
    try:
        return jsonify(lomax.restore(run_id))
    except lomax.LomaxError as e:
        return _err(e)


@bp.route("/usage", methods=["GET"])
@login_required
def usage():
    return jsonify(lomax.usage_log(request.args.get("limit", 50, type=int), request.args.get("offset", 0, type=int)))


@bp.route("/latest", methods=["GET"])
@login_required
def latest():
    skill, stype = request.args.get("skill"), request.args.get("subject_type")
    sid = request.args.get("subject_id", type=int)
    skill = "recording" if skill == "resolution" else skill
    if skill not in lomax.SKILLS or not stype or not sid:
        return jsonify({"error": "skill, subject_type and subject_id are required"}), 400
    return jsonify(lomax.latest_run(skill, stype, sid))


@bp.route("/proposals/<int:proposal_id>", methods=["POST"])
@login_required
def decide(proposal_id):
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(lomax.decide(proposal_id, data.get("decision"), current_user.id))
    except (lomax.LomaxError, lomax.ApplyError) as e:
        return _err(e)


@bp.route("/estimate", methods=["GET"])
@login_required
def estimate():
    try:
        return jsonify(lomax.estimate(request.args.get("skill"), request.args.get("level") or "research",
                                      request.args.get("question") == "1"))
    except lomax.LomaxError as e:
        return _err(e)
