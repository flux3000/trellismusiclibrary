"""
api/collections.py — Collections: optional user-defined groupings of Recordings
(many-to-many). CRUD + add/remove a recording.
"""

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app.extensions import db
from app.models.collection import Collection, CollectionRecording
from app.models.recording import Recording
from app.utils.serialize import recording_row

bp = Blueprint("collections", __name__)


def _reject_system(c, what):
    """System collections are dynamic; their membership is a query, not rows.

    Deleting one is the dangerous case: peer grants point at a collection id, so
    dropping "Full Library" would revoke every Streamer at once, silently, with
    nothing on screen to explain why their library went empty.

    The label is a different matter — renaming a system collection is harmless,
    so name/description stay editable and only membership and deletion refuse.
    """
    if c is not None and c.is_system:
        return jsonify({
            "error": f"{c.name} is a system collection — {what}.",
        }), 409
    return None


@bp.route("/")
@login_required
def list_collections():
    cols = db.session.query(Collection).order_by(Collection.name).all()
    # recording_count comes from the model, which resolves dynamic membership.
    # `len(c.recording_links)` would report 0 for every system collection.
    return jsonify([
        {"id": c.id, "name": c.name, "description": c.description,
         "recording_count": c.recording_count,
         "is_system": c.is_system, "system_key": c.system_key}
        for c in cols
    ])


@bp.route("/<int:collection_id>")
@login_required
def get_collection(collection_id):
    c = db.session.get(Collection, collection_id)
    if not c:
        return jsonify({"error": "Not found"}), 404
    # card=True: the collection page renders handbill cards (Ryan, 2026-08-07),
    # which need the performer's genre colour and primary photo. Collections
    # are small — a few dozen rows — so the extra joins are cheap here in a way
    # they would not be on the 544-row flat List.
    #
    # ⚠ PERF: for a system collection this is the whole library — 580 rows with
    # card eager-loads, which is precisely the case the comment above says is
    # NOT cheap. The frontend should route a system collection to Browse rather
    # than render it as a collection page; this endpoint stays honest rather
    # than lying about its contents, but it is not a page to link casually.
    rows = [recording_row(r, card=True) for r in c.recordings]
    rows.sort(key=lambda r: ((r["performer"] or "").lower(),
                             r["start_year"] or 0, r["start_month"] or 0, r["start_day"] or 0))
    return jsonify({"id": c.id, "name": c.name, "description": c.description,
                    "is_system": c.is_system, "system_key": c.system_key,
                    "recordings": rows})


@bp.route("/", methods=["POST"])
@login_required
def create_collection():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    c = Collection(name=name, description=data.get("description"))
    db.session.add(c)
    db.session.commit()
    return jsonify({"id": c.id, "name": c.name}), 201


@bp.route("/<int:collection_id>", methods=["PUT"])
@login_required
def update_collection(collection_id):
    c = db.session.get(Collection, collection_id)
    if not c:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    for f in ["name", "description"]:
        if f in data:
            setattr(c, f, data[f])
    db.session.commit()
    return jsonify({"id": c.id})


@bp.route("/<int:collection_id>", methods=["DELETE"])
@login_required
def delete_collection(collection_id):
    c = db.session.get(Collection, collection_id)
    if c:
        refusal = _reject_system(c, "it cannot be deleted while peers hold grants to it")
        if refusal:
            return refusal
        db.session.delete(c)
        db.session.commit()
    return jsonify({"ok": True})


@bp.route("/<int:collection_id>/recordings", methods=["POST"])
@login_required
def add_recording(collection_id):
    c = db.session.get(Collection, collection_id)
    if not c:
        return jsonify({"error": "Not found"}), 404
    refusal = _reject_system(c, "its contents are determined by a query, not by hand")
    if refusal:
        return refusal
    rid = (request.get_json() or {}).get("recording_id")
    if not db.session.get(Recording, rid):
        return jsonify({"error": "recording not found"}), 404
    exists = db.session.query(CollectionRecording).filter_by(
        collection_id=collection_id, recording_id=rid).first()
    if not exists:
        n = db.session.query(CollectionRecording).filter_by(collection_id=collection_id).count()
        db.session.add(CollectionRecording(collection_id=collection_id, recording_id=rid, order=n))
        db.session.commit()
    return jsonify({"ok": True})


@bp.route("/<int:collection_id>/recordings/<int:recording_id>", methods=["DELETE"])
@login_required
def remove_recording(collection_id, recording_id):
    c = db.session.get(Collection, collection_id)
    refusal = _reject_system(c, "its contents are determined by a query, not by hand")
    if refusal:
        return refusal
    link = db.session.query(CollectionRecording).filter_by(
        collection_id=collection_id, recording_id=recording_id).first()
    if link:
        db.session.delete(link)
        db.session.commit()
    return jsonify({"ok": True})
