"""
api/venues.py — Venue endpoints.

Routes:
  GET  /api/venues/        — list venues (q= for search)
  GET  /api/venues/<id>    — venue detail + performances
  POST /api/venues/        — create venue
  PUT  /api/venues/<id>    — update venue
"""

from pathlib import Path

from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required
from sqlalchemy import or_

from app.extensions import db
from app.models.venue import Venue
from app.models.venue_image import VenueImage
from app.utils.serialize import recording_row
from app.utils.ingest import _sanitize_path
from app.utils import entity_images as ei
from app.api.system import require_library

bp = Blueprint("venues", __name__)


@bp.route("/")
@login_required
def list_venues():
    q = request.args.get("q", "").strip()
    query = db.session.query(Venue)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(Venue.name.ilike(like), Venue.city.ilike(like), Venue.state.ilike(like))
        )
    # No limit — list_performers/list_artists/list_genres don't cap theirs
    # either, and the sidebar's whole point is showing the complete index.
    # This one WAS capped at 200 and it bit: alphabetically past "L" is
    # exactly where a 200-row cut lands once a library has more than 200
    # venues, so the Venues nav list silently stopped mid-alphabet with no
    # sign anything was missing (Ryan, 2026-08-30).
    venues = query.order_by(Venue.name).all()
    return jsonify([
        {
            "id":                v.id,
            "name":              v.name,
            "city":              v.city,
            "state":             v.state,
            "country":           v.country,
            "performance_count": len(v.performances),
        }
        for v in venues
    ])


@bp.route("/<int:venue_id>")
@login_required
def get_venue(venue_id):
    v = db.session.get(Venue, venue_id)
    if not v:
        return jsonify({"error": "Not found"}), 404

    perfs = sorted(
        v.performances,
        key=lambda p: (p.start_year or 0, p.start_month or 0, p.start_day or 0),
    )

    # One row per Recording at this venue (two tapers of one show = two rows),
    # chronological old→new. Recordings within a performance keep their own order.
    recordings = [recording_row(r) for p in perfs for r in p.recordings]

    return jsonify({
        "id":                v.id,
        "name":              v.name,
        "city":              v.city,
        "state":             v.state,
        "country":           v.country,
        "bio":               v.bio,
        "performance_count": len(v.performances),
        "recording_count":   len(recordings),
        "recordings":        recordings,
        "has_image":         bool(v.images),
        "images":            [ei.image_payload(i, _IMG_URL) for i in v.images],
    })


# ── Photos (2026-08-07) ──────────────────────────────────────────────────────
# Parallel table to performer_image, shared behaviour via utils/entity_images.
# Files live under LIBRARY_ROOT/_venues/<sanitized name>/_images — the `_venues`
# prefix keeps them out of the performer namespace, because a venue and an act
# can share a name ("Fillmore") and two entities writing one folder is a
# collision waiting to happen.

_IMG_URL = "/api/venues/images"


def _venue_images_dir(venue):
    library_root = current_app.config["LIBRARY_ROOT"]
    return Path(library_root) / "_venues" / _sanitize_path(venue.name) / "_images"


@bp.route("/<int:venue_id>/images", methods=["GET"])
@login_required
def list_venue_images(venue_id):
    v = db.session.get(Venue, venue_id)
    if not v:
        return jsonify({"error": "Not found"}), 404
    return jsonify([ei.image_payload(i, _IMG_URL) for i in v.images])


@bp.route("/<int:venue_id>/images", methods=["POST"])
@login_required
def upload_venue_images(venue_id):
    v = db.session.get(Venue, venue_id)
    if not v:
        return jsonify({"error": "Not found"}), 404
    return ei.handle_upload(v, VenueImage, _venue_images_dir(v), _IMG_URL)


@bp.route("/images/<int:image_id>", methods=["GET"])
@login_required
@require_library(kind="image")
def serve_venue_image(image_id):
    img = db.session.get(VenueImage, image_id)
    if not img:
        return jsonify({"error": "No image"}), 404
    return ei.handle_serve(img, _venue_images_dir(img.venue))


@bp.route("/images/<int:image_id>/primary", methods=["POST"])
@login_required
def make_venue_image_primary(image_id):
    img = db.session.get(VenueImage, image_id)
    if not img:
        return jsonify({"error": "Not found"}), 404
    ei.set_primary(img)
    db.session.commit()
    return jsonify(ei.image_payload(img, _IMG_URL))


@bp.route("/images/<int:image_id>", methods=["DELETE"])
@login_required
def delete_venue_image(image_id):
    img = db.session.get(VenueImage, image_id)
    if not img:
        return jsonify({"error": "Not found"}), 404
    return ei.handle_delete(img, _venue_images_dir(img.venue))


@bp.route("/", methods=["POST"])
@login_required
def create_venue():
    data = request.get_json()
    if not data.get("name", "").strip():
        return jsonify({"error": "name is required"}), 400
    v = Venue(
        name    = data["name"].strip(),
        city    = data.get("city", "").strip() or None,
        state   = data.get("state", "").strip() or None,
        country = data.get("country", "").strip() or None,
        bio     = data.get("bio", "").strip() or None,
    )
    db.session.add(v)
    db.session.commit()
    return jsonify({"id": v.id, "name": v.name}), 201


@bp.route("/<int:venue_id>", methods=["PUT"])
@login_required
def update_venue(venue_id):
    v = db.session.get(Venue, venue_id)
    if not v:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json()
    for field in ["name", "city", "state", "country", "bio"]:
        if field in data:
            setattr(v, field, data[field])
    db.session.commit()
    return jsonify({"id": v.id})


@bp.route("/<int:venue_id>", methods=["DELETE"])
@login_required
def delete_venue(venue_id):
    """Delete a venue. Refuses while performances still reference it."""
    v = db.session.get(Venue, venue_id)
    if not v:
        return jsonify({"error": "Not found"}), 404
    n = len(v.performances)
    if n:
        return jsonify({"error": f"Venue has {n} performance(s) — reassign or "
                                 "delete those recordings first."}), 409
    db.session.delete(v)
    db.session.commit()
    return jsonify({"ok": True})
