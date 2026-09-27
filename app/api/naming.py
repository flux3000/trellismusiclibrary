"""
api/naming.py — the naming engine's one HTTP surface.

Lives beside app/utils/file_naming.py rather than in system.py so the engine
and its preview move together (spec section 11).

Routes:
  GET /api/naming/preview?scheme=&template=&recording_id=
"""

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app.extensions import db
from app.models.recording import Recording
from app.models.track import Track
from app.utils.file_naming import rename_plan, TemplateError

bp = Blueprint("naming", __name__)


def _default_recording_id():
    """
    No recording_id given: the most recent recording with a disc when one
    exists, else the most recent recording overall (spec section 4 —
    Settings picks the fixture this way so the preview is populated).
    """
    with_disc = (
        db.session.query(Recording)
        .join(Track, Track.recording_id == Recording.id)
        .filter(Track.disc_number.isnot(None))
        .order_by(Recording.created_at.desc())
        .first()
    )
    if with_disc:
        return with_disc.id
    latest = db.session.query(Recording).order_by(Recording.created_at.desc()).first()
    return latest.id if latest else None


@bp.route("/preview")
@login_required
def preview():
    scheme      = request.args.get("scheme", "original")
    template    = request.args.get("template")
    recording_id = request.args.get("recording_id", type=int)

    if recording_id is None:
        recording_id = _default_recording_id()
    if recording_id is None:
        return jsonify({"plan": []})

    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    try:
        plan = rename_plan(rec, scheme, template=template)
    except TemplateError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({
        "recording_id": rec.id,
        "plan": [
            {"track_id": t.id, "current": old, "proposed": new}
            for t, old, new in plan
        ],
    })
