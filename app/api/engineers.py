"""
api/engineers.py -- Engineer endpoints: search, list, detail with the recordings
they are credited on (and in which role), create, update, delete, aliases and
photos. Modelled on api/musicians.py. Engineers are credited on LIVE recordings
only; nothing here ever returns a studio record.
"""

from flask import Blueprint, jsonify, request
from flask_login import login_required
from sqlalchemy import func

from app.extensions import db
from app.models.engineer import Engineer, EngineerImage
from app.models.alias import EngineerAlias
from app.models.recording import Recording
from app.utils.serialize import recording_row
from app.utils.published import list_all_requested, entity_listed
from app.utils import entity_images as ei
from app.utils.engineers import engineer_key, find_engineer, engineer_role_rows
from app.api.system import require_library

bp = Blueprint("engineers", __name__)

_IMG_URL = "/api/engineers/images"


def _engineer_images_dir(engineer):
    # DATA_DIR/images/engineers/<sanitized name>
    return ei.image_dir("engineers", engineer.name)


ei.register_image_routes(
    bp, parent_model=Engineer, image_model=EngineerImage, url_prefix=_IMG_URL,
    images_dir_for=_engineer_images_dir,
    login_required=login_required, require_library=require_library,
)


def _alias_payload(a):
    return {"id": a.id, "alias": a.alias}


@bp.route("/search")
@login_required
def search_engineers():
    """Engineer-name autocomplete (matches the name or any alias)."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    by_alias = (db.session.query(EngineerAlias.engineer_id)
                .filter(EngineerAlias.alias.ilike(f"%{q}%")))
    rows = (db.session.query(Engineer)
            .filter(Engineer.name.ilike(f"%{q}%") | Engineer.id.in_(by_alias))
            .order_by(Engineer.name).limit(12).all())
    return jsonify([{"id": e.id, "name": e.name} for e in rows])


@bp.route("/")
@login_required
def list_engineers():
    rows = db.session.query(Engineer).order_by(
        func.coalesce(Engineer.sort_name, Engineer.name)).all()

    # Live recordings credited in any role, grouped (the sidebar reads this on
    # every render, so no per-row queries).
    counts, totals = {}, {}
    for *r, pub in (db.session.query(Recording.recording_engineer_id,
                                     Recording.transfer_engineer_id,
                                     Recording.mastering_engineer_id,
                                     Recording.is_published)
                    .filter(Recording.kind != "studio")):
        for eid in {x for x in r if x is not None}:
            totals[eid] = totals.get(eid, 0) + 1
            if pub:
                counts[eid] = counts.get(eid, 0) + 1
    show_all = list_all_requested(request.args)

    extra = {}
    if request.args.get("index"):
        from app.utils import dim_index
        extra = dim_index.engineer_extras()

    return jsonify([
        {**extra.get(e.id, {}),
         "id": e.id, "name": e.name, "sort_name": e.sort_name,
         "recording_count": counts.get(e.id, 0)}
        for e in rows
        if entity_listed(counts.get(e.id), totals.get(e.id), show_all)
    ])


@bp.route("/<int:engineer_id>")
@login_required
def get_engineer(engineer_id):
    """An engineer + every live recording they are credited on. Each recording
    row is a recording_row plus `roles`: a list of "recording" /
    "transfer" / "mastering"."""
    e = db.session.get(Engineer, engineer_id)
    if not e:
        return jsonify({"error": "Not found"}), 404
    recordings = [{**recording_row(r), "roles": roles}
                  for r, roles in engineer_role_rows(engineer_id) if r.is_published]
    return jsonify({
        "id":         e.id,
        "name":       e.name,
        "sort_name":  e.sort_name,
        "bio":        e.bio,
        "aliases":    [_alias_payload(a) for a in e.aliases],
        "recording_count": len(recordings),
        "recordings": recordings,
        "has_image":  bool(e.images),
        "images":     [ei.image_payload(i, _IMG_URL) for i in e.images],
    })


@bp.route("/", methods=["POST"])
@login_required
def create_engineer():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    existing = find_engineer(name)
    if existing:
        return jsonify({"id": existing.id, "name": existing.name}), 200
    e = Engineer(name=name, sort_name=data.get("sort_name"), bio=data.get("bio"))
    db.session.add(e)
    db.session.commit()
    return jsonify({"id": e.id, "name": e.name}), 201


@bp.route("/<int:engineer_id>", methods=["PUT"])
@login_required
def update_engineer(engineer_id):
    e = db.session.get(Engineer, engineer_id)
    if not e:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    if "name" in data and not (data["name"] or "").strip():
        return jsonify({"error": "name is required"}), 400
    for f in ["name", "sort_name", "bio"]:
        if f in data:
            setattr(e, f, data[f].strip() if f == "name" else data[f])
    db.session.commit()
    return jsonify({"id": e.id})


@bp.route("/<int:engineer_id>", methods=["DELETE"])
@login_required
def delete_engineer(engineer_id):
    """Delete an engineer. Recordings that credited them just lose the credit."""
    e = db.session.get(Engineer, engineer_id)
    if not e:
        return jsonify({"error": "Not found"}), 404
    # Clear the FKs explicitly rather than lean on ON DELETE SET NULL: a database
    # upgraded by ALTER TABLE, or a session without the FK pragma, must behave the same.
    for col in (Recording.recording_engineer_id, Recording.transfer_engineer_id,
                Recording.mastering_engineer_id):
        db.session.query(Recording).filter(col == engineer_id).update(
            {col.key: None}, synchronize_session=False)
    db.session.delete(e)
    db.session.commit()
    return jsonify({"ok": True})


# ── Aliases ────────────────────────────────────────────────────────────────

@bp.route("/<int:engineer_id>/aliases", methods=["GET"])
@login_required
def list_aliases(engineer_id):
    e = db.session.get(Engineer, engineer_id)
    if not e:
        return jsonify({"error": "Not found"}), 404
    return jsonify([_alias_payload(a) for a in e.aliases])


@bp.route("/<int:engineer_id>/aliases", methods=["POST"])
@login_required
def add_alias(engineer_id):
    e = db.session.get(Engineer, engineer_id)
    if not e:
        return jsonify({"error": "Not found"}), 404
    alias = ((request.get_json() or {}).get("alias") or "").strip()
    key = engineer_key(alias)
    if not key:
        return jsonify({"error": "alias is required"}), 400
    if key == engineer_key(e.name):
        return jsonify({"error": "Same as the name"}), 409
    other = find_engineer(alias)
    if other is not None and other.id != e.id:
        return jsonify({"error": f"Already used by {other.name}"}), 409
    for a in e.aliases:
        if engineer_key(a.alias) == key:
            return jsonify(_alias_payload(a)), 200
    a = EngineerAlias(engineer_id=e.id, alias=alias[:255], alias_key=key[:255])
    db.session.add(a)
    db.session.commit()
    return jsonify(_alias_payload(a)), 201


@bp.route("/<int:engineer_id>/aliases/<int:alias_id>", methods=["DELETE"])
@login_required
def delete_alias(engineer_id, alias_id):
    a = db.session.get(EngineerAlias, alias_id)
    if not a or a.engineer_id != engineer_id:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(a)
    db.session.commit()
    return jsonify({"ok": True})
