"""
api/genres.py — Genre endpoints.

Genre is a proper dimension (see the Genre design spec in Context Library,
2026-08-02): its own table, one FK from Artist, guarded delete — matching
how Venue, Collection and Musician deletes already behave. Nothing here ever
creates a genre implicitly; every picker in the frontend selects from this
list only.

Routes:
  GET    /api/genres/         — list genres (q= for search)
  GET    /api/genres/<id>     — genre detail: its artists + their recordings
  POST   /api/genres/         — create genre
  PUT    /api/genres/<id>     — update genre
  DELETE /api/genres/<id>     — delete genre (409 while referenced)
"""

from flask import Blueprint, request, jsonify
from flask_login import login_required
from sqlalchemy import func

from app.extensions import db
from app.models.genre import Genre
from app.models.artist import Artist
from app.models.performance import Performance
from app.models.recording import Recording
from app.utils.serialize import recording_summary

bp = Blueprint("genres", __name__)


@bp.route("/")
@login_required
def list_genres():
    q = request.args.get("q", "").strip()
    query = db.session.query(Genre)
    if q:
        query = query.filter(Genre.name.ilike(f"%{q}%"))
    genres = query.order_by(Genre.name).all()

    artist_counts = dict(
        db.session.query(Artist.genre_id, func.count(Artist.id))
        .filter(Artist.genre_id.isnot(None))
        .group_by(Artist.genre_id).all()
    )
    recording_counts = dict(
        db.session.query(Artist.genre_id, func.count(Recording.id))
        .join(Performance, Performance.artist_id == Artist.id)
        .join(Recording, Recording.performance_id == Performance.id)
        .filter(Artist.genre_id.isnot(None))
        .group_by(Artist.genre_id).all()
    )
    return jsonify([
        {
            "id":              g.id,
            "name":            g.name,
            "description":     g.description,
            "color":           g.color,
            "artist_count": artist_counts.get(g.id, 0),
            "recording_count": recording_counts.get(g.id, 0),
        }
        for g in genres
    ])


# `#rrggbb` only — the colour picker emits exactly this, and accepting shorthand
# or named colours would mean every consumer (card flair, chips, future exports)
# needs its own normalisation. One canonical form, validated at the door.
_HEX_RE = __import__("re").compile(r"^#[0-9a-fA-F]{6}$")


def _clean_color(raw):
    """
    Normalise a submitted colour to lowercase `#rrggbb`, or None to clear it.

    Returns (value, error). Clearing is explicitly supported: NULL is a real
    state that renders neutral grey, not a validation failure.
    """
    if raw is None:
        return None, None
    s = str(raw).strip()
    if not s:
        return None, None
    if not _HEX_RE.match(s):
        return None, f"color must be #rrggbb hex, got {s!r}"
    return s.lower(), None


@bp.route("/<int:genre_id>")
@login_required
def get_genre(genre_id):
    g = db.session.get(Genre, genre_id)
    if not g:
        return jsonify({"error": "Not found"}), 404

    artists = (
        db.session.query(Artist)
        .filter(Artist.genre_id == genre_id)
        .order_by(func.coalesce(Artist.sort_name, Artist.name))
        .all()
    )
    perf_rows = []
    total_recordings = 0
    for p in artists:
        performances = (
            db.session.query(Performance)
            .filter(Performance.artist_id == p.id)
            .order_by(
                Performance.start_year.desc().nullsfirst(),
                Performance.start_month.desc().nullsfirst(),
                Performance.start_day.desc().nullsfirst(),
            ).all()
        )
        # Flatten to one row per Recording, decorated with the performance's
        # date/venue — same shape the Artist page's flat recording table
        # expects (see get_artist_recordings / all_recordings).
        recordings = []
        for perf in performances:
            v = perf.venue
            for r in perf.recordings:
                row = recording_summary(r)
                row.update({
                    "artist":   p.name,
                    "start_year":  perf.start_year,
                    "start_month": perf.start_month,
                    "start_day":   perf.start_day,
                    "venue":       v.name    if v else None,
                    "city":        v.city    if v else perf.city,
                    "state":       v.state   if v else perf.state,
                    "country":     v.country if v else perf.country,
                })
                recordings.append(row)
        total_recordings += len(recordings)
        perf_rows.append({
            "id":              p.id,
            "name":            p.name,
            "recording_count": len(recordings),
            "recordings":      recordings,
        })

    return jsonify({
        "id":              g.id,
        "name":            g.name,
        "description":     g.description,
        "color":           g.color,
        "artist_count": len(perf_rows),
        "recording_count": total_recordings,
        "artists":      perf_rows,
    })


@bp.route("/", methods=["POST"])
@login_required
def create_genre():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if db.session.query(Genre).filter(func.lower(Genre.name) == name.lower()).first():
        return jsonify({"error": "Genre already exists"}), 409
    color, err = _clean_color(data.get("color"))
    if err:
        return jsonify({"error": err}), 400
    g = Genre(name=name, description=(data.get("description") or "").strip() or None,
              color=color)
    db.session.add(g)
    db.session.commit()
    return jsonify({"id": g.id, "name": g.name, "color": g.color}), 201


@bp.route("/<int:genre_id>", methods=["PUT"])
@login_required
def update_genre(genre_id):
    g = db.session.get(Genre, genre_id)
    if not g:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 400
        dup = db.session.query(Genre).filter(
            func.lower(Genre.name) == name.lower(), Genre.id != genre_id).first()
        if dup:
            return jsonify({"error": "Genre already exists"}), 409
        g.name = name
    if "description" in data:
        g.description = (data["description"] or "").strip() or None
    if "color" in data:
        color, err = _clean_color(data["color"])
        if err:
            return jsonify({"error": err}), 400
        g.color = color
    db.session.commit()
    return jsonify({"id": g.id, "color": g.color})


@bp.route("/<int:genre_id>", methods=["DELETE"])
@login_required
def delete_genre(genre_id):
    """Delete a genre. Refuses while artists still reference it."""
    g = db.session.get(Genre, genre_id)
    if not g:
        return jsonify({"error": "Not found"}), 404
    n = db.session.query(Artist).filter_by(genre_id=genre_id).count()
    if n:
        return jsonify({"error": f"Genre has {n} artist(s) — reassign or "
                                 "clear those first."}), 409
    db.session.delete(g)
    db.session.commit()
    return jsonify({"ok": True})
