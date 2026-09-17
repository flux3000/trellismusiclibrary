"""
api/musicians.py — Musician (person) endpoints: search, list, and person→artists
aggregation (the "everything by Béla Fleck" view). Used to pick members in the
Add/Edit forms.
"""

from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required
from sqlalchemy import func

from app.extensions import db
from app.models.musician import Musician, Membership
from app.models.musician_image import MusicianImage
from app.models.artist import Artist
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.performance_personnel import PerformancePersonnel
from app.utils.format import format_partial_date
from app.utils.serialize import recording_summary
from app.utils.artists import resolve_or_create_artist
from app.utils.ingest import _sanitize_path
from app.utils import entity_images as ei
from app.api.system import require_library

bp = Blueprint("musicians", __name__)


# ── Photos (2026-09-01) ──────────────────────────────────────────────────────
# Fourth photographed entity. Routes are generated, not written — see
# ei.register_image_routes() for why, and models/musician_image.py for why this
# reverses the 2026-08-07 "artist-level only" call for the PAGE while
# leaving it standing for cards (a card keys off the act; a wall of identical
# tiles is still the failure mode it was protecting against).

_IMG_URL = "/api/musicians/images"


def _musician_images_dir(musician):
    # LIBRARY_ROOT/_musicians/<sanitized person name>/_images. The bucket matters:
    # a person and an act share a name constantly (Bill Evans, Doc Watson), and
    # artist photos live at the library root with no prefix at all.
    return ei.entity_images_dir(current_app.config["LIBRARY_ROOT"],
                                "_musicians", musician.name, _sanitize_path)


ei.register_image_routes(
    bp, parent_model=Musician, image_model=MusicianImage, url_prefix=_IMG_URL,
    images_dir_for=_musician_images_dir,
    login_required=login_required, require_library=require_library,
)


@bp.route("/search")
@login_required
def search_artists():
    """Person-name autocomplete for the Members multi-select."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    rows = (db.session.query(Musician)
            .filter(Musician.name.ilike(f"%{q}%"))
            .order_by(Musician.name).limit(12).all())
    return jsonify([{"id": a.id, "name": a.name} for a in rows])


@bp.route("/")
@login_required
def list_musicians():
    rows = db.session.query(Musician).order_by(
        func.coalesce(Musician.sort_name, Musician.name)).all()

    # ⚠ sort_name is NULL for every row in Ryan's library (CONTEXT.md trap —
    # scripts/backfill_sort_names.py has never been run here), so COALESCE is
    # load-bearing rather than decorative. Do not "simplify" it away.

    # Counts and the primary-photo id, for the Musicians index page. Grouped
    # queries, not one lazy-load per person: the sidebar and the index both
    # read this endpoint, and it is called on every sidebar render.
    #
    # A person's recordings are reached through the acts they are a member of,
    # one hop further than a venue's — hence the join through membership.
    # Guest-only sit-ins (performance_personnel rows on acts they are NOT a
    # member of) are deliberately NOT counted here: they are counted on the
    # detail page as a separate "Guest" stat, because folding them in would
    # make a one-night sit-in read as a body of work.
    rec_counts = dict(
        db.session.query(Membership.musician_id, func.count(Recording.id))
        .join(Performance, Performance.artist_id == Membership.artist_id)
        .join(Recording, Recording.performance_id == Performance.id)
        .group_by(Membership.musician_id).all()
    )
    perf_counts = dict(
        db.session.query(Membership.musician_id,
                         func.count(func.distinct(Membership.artist_id)))
        .group_by(Membership.musician_id).all()
    )
    image_ids = {}
    for aid, iid, _p in (
        db.session.query(MusicianImage.musician_id, MusicianImage.id, MusicianImage.is_primary)
        .order_by(MusicianImage.musician_id, MusicianImage.is_primary.desc(),
                  MusicianImage.sort_order, MusicianImage.id).all()
    ):
        image_ids.setdefault(aid, iid)

    return jsonify([
        {
            "id":              a.id,
            "name":            a.name,
            "sort_name":       a.sort_name,
            "recording_count": rec_counts.get(a.id, 0),
            "artist_count": perf_counts.get(a.id, 0),
            "image_id":        image_ids.get(a.id),
        }
        for a in rows
    ])


@bp.route("/<int:musician_id>")
@login_required
def get_musician(musician_id):
    """
    A person + every Artist (act) they're a member of, PLUS individual
    show-level appearances reached only via performance_personnel — sit-ins
    or explicit-mode picks on acts they're not formally a Membership of at
    all (2026-07-18, Per-Show Personnel design doc ripple item 3: "Béla's
    page would finally surface his All-Stars sit-ins"). Kept as a separate
    `guest_appearances` list rather than folded into `artists`, since the
    existing UI pulls EVERY recording of a listed artist — doing that for
    a one-off sit-in would misrepresent a single show as full membership.
    """
    a = db.session.get(Musician, musician_id)
    if not a:
        return jsonify({"error": "Not found"}), 404
    # Skip any dangling membership whose artist was removed.
    artists = [m.artist for m in a.memberships if m.artist is not None]
    artists.sort(key=lambda p: (p.sort_name or p.name).lower())
    member_artist_ids = {p.id for p in artists}

    guest_appearances = []
    for pp in db.session.query(PerformancePersonnel).filter_by(musician_id=musician_id).all():
        perf = pp.performance
        if not perf or perf.artist_id in member_artist_ids:
            continue   # already covered by the act-membership list above
        v = perf.venue
        guest_appearances.append({
            "performance_id": perf.id,
            "artist_id":   perf.artist_id,
            "artist_name": perf.artist.name if perf.artist else None,
            "date":       format_partial_date(perf.start_year, perf.start_month, perf.start_day),
            # Split date + location, matching the shape artists.recordings
            # already returns, so the frontend can render these with the same
            # flatRowHtml() row builder instead of a bespoke one.
            "start_year": perf.start_year, "start_month": perf.start_month, "start_day": perf.start_day,
            "venue_name": v.name    if v else None,
            "city":       v.city    if v else perf.city,
            "state":      v.state   if v else perf.state,
            "country":    v.country if v else perf.country,
            "instrument": pp.instrument,
            "is_guest":   pp.is_guest,
            "note":       pp.note,
            "recordings": [recording_summary(r) for r in perf.recordings],
        })
    guest_appearances.sort(key=lambda g: (g["start_year"] or 0, g["start_month"] or 0, g["start_day"] or 0))

    return jsonify({
        "id":         a.id,
        "name":       a.name,
        "sort_name":  a.sort_name,
        "bio":        a.bio,
        "artists":         [{"id": p.id, "name": p.name} for p in artists],
        "guest_appearances":  guest_appearances,
        "has_image":          bool(a.images),
        "images":             [ei.image_payload(i, _IMG_URL) for i in a.images],
    })


@bp.route("/", methods=["POST"])
@login_required
def create_musician():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    existing = db.session.query(Musician).filter(func.lower(Musician.name) == name.lower()).first()
    if existing:
        return jsonify({"id": existing.id, "name": existing.name}), 200
    a = Musician(name=name, sort_name=data.get("sort_name"), bio=data.get("bio"))
    db.session.add(a)
    db.session.commit()
    return jsonify({"id": a.id, "name": a.name}), 201


@bp.route("/<int:musician_id>", methods=["PUT"])
@login_required
def update_musician(musician_id):
    a = db.session.get(Musician, musician_id)
    if not a:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json()
    for f in ["name", "sort_name", "bio"]:
        if f in data:
            setattr(a, f, data[f])
    db.session.commit()
    return jsonify({"id": a.id})


@bp.route("/<int:musician_id>", methods=["DELETE"])
@login_required
def delete_musician(musician_id):
    """Delete a person. Refuses while they're still a member of any Artist —
    remove them from those acts first."""
    a = db.session.get(Musician, musician_id)
    if not a:
        return jsonify({"error": "Not found"}), 404
    n = len(a.memberships)
    if n:
        return jsonify({"error": f"Musician is a member of {n} artist(s) — "
                                 "remove them from those acts first."}), 409
    db.session.delete(a)
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/<int:musician_id>/artists", methods=["POST"])
@login_required
def add_artist_association(musician_id):
    """Associate this person with an Artist (by id or name; creates the
    artist if only a new name is given). Appends to the act's roster."""
    a = db.session.get(Musician, musician_id)
    if not a:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    pid  = data.get("artist_id")
    if pid:
        artist = db.session.get(Artist, pid)
        if not artist:
            return jsonify({"error": "artist not found"}), 404
    else:
        name = (data.get("artist_name") or "").strip()
        if not name:
            return jsonify({"error": "artist_id or artist_name required"}), 400
        artist = resolve_or_create_artist(name)

    exists = db.session.query(Membership).filter_by(
        artist_id=artist.id, musician_id=musician_id).first()
    if not exists:
        order = db.session.query(Membership).filter_by(artist_id=artist.id).count()
        db.session.add(Membership(artist_id=artist.id, musician_id=musician_id, order=order))
    db.session.commit()
    return jsonify({"id": artist.id, "name": artist.name})


@bp.route("/<int:musician_id>/artists/<int:artist_id>", methods=["DELETE"])
@login_required
def remove_artist_association(musician_id, artist_id):
    """Remove this person from an Artist's roster (the artist itself stays)."""
    db.session.query(Membership).filter_by(
        artist_id=artist_id, musician_id=musician_id).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({"ok": True})
