"""
api/performers.py — Performer (act) endpoints: browse, catalog, search, members.

A Performer is the act you browse and tag by. Its member Artists (people) are
managed here too. Grouping "everything by a person" lives on the Artist side
(api/artists.py), not here.
"""

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

import json

from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.performer import Performer, PerformerResource
from app.models.performer_image import PerformerImage
from app.utils.entity_images import set_primary
from app.models.genre import Genre
from app.models.artist import Artist, Membership
from app.models.performance import Performance
from app.models.recording import Recording
from app.utils.serialize import recording_summary
from app.utils.ingest import _sanitize_path
from app.utils.performers import (
    set_performer_members, add_membership_stint,
    update_membership_stint_bounds, remove_membership_stint,
)
from app.utils import musicbrainz, commons
from app.utils import entity_images as ei
from app.utils.performer_research import run_performer_research
from app.utils.ai_assist import AiAssistError
from app.utils.prefs import get_api_key, get_pref
from app.api.system import require_library

bp = Blueprint("performers", __name__)

# Canonical list lives in utils/entity_images so both image tables accept the
# same formats. Aliased here for the Commons fetch path below.
_ALLOWED_IMAGE_EXTS = ei.ALLOWED_IMAGE_EXTS


def _performer_images_dir(performer):
    """
    LIBRARY_ROOT/<sanitized name>/_images — the leading underscore sorts it
    first alongside/before recording folders in a Finder listing (Ryan,
    2026-07-22). NOTE: derived from the Performer's CURRENT name at request
    time, not a stored path — see Performer.image_ext's docstring for the
    rename-orphan caveat this carries (matches how existing recording
    folders already behave on a rename: nothing moves those either).
    """
    library_root = current_app.config["LIBRARY_ROOT"]
    return Path(library_root) / _sanitize_path(performer.name) / "_images"


def _serialize_roster(performer):
    """
    Member Artists deduped by person (see Performer.artists), each carrying
    their stint row(s) — usually one unbounded row ('always a member'), but
    possibly several for someone with real tenure gaps (Mickey Hart). Powers
    the Performer page's stint editor.
    """
    by_artist = {}
    for m in performer.memberships:   # already ordered by Membership.order
        by_artist.setdefault(m.artist_id, []).append(m)
    roster = []
    for artist_id, stints in by_artist.items():
        roster.append({
            "id":   artist_id,
            "name": stints[0].artist.name,
            "stints": [
                {
                    "id": s.id,
                    "start_year": s.start_year, "start_month": s.start_month,
                    "start_day":  s.start_day,
                    "end_year":   s.end_year,   "end_month":   s.end_month,
                    "end_day":    s.end_day,
                }
                for s in sorted(stints, key=lambda s: s.order)
            ],
        })
    return roster


@bp.route("/search")
@login_required
def search_performers():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    rows = (db.session.query(Performer)
            .filter(Performer.name.ilike(f"%{q}%"))
            .order_by(Performer.name).limit(12).all())
    return jsonify([{"id": p.id, "name": p.name} for p in rows])


@bp.route("/")
@login_required
def list_performers():
    """All performers with recording counts + member names — powers the sidebar."""
    rows = (
        db.session.query(Performer, func.count(Recording.id).label("rc"))
        .outerjoin(Performance, Performance.performer_id == Performer.id)
        .outerjoin(Recording,   Recording.performance_id == Performance.id)
        .group_by(Performer.id)
        .order_by(func.coalesce(Performer.sort_name, Performer.name))
        .all()
    )
    return jsonify([
        {
            "id":              p.id,
            "name":            p.name,
            "sort_name":       p.sort_name,
            "recording_count": rc,
            "members":         [a.name for a in p.artists],
            # Genre (2026-08-02) — id + name only, powers the bulk assignment
            # screen's "unassigned only" filter and pre-fill.
            "genre_id":        p.genre_id,
            "genre_name":      p.genre.name if p.genre else None,
        }
        for p, rc in rows
    ])


@bp.route("/all-recordings")
@login_required
def all_recordings():
    """
    Every performer (alpha) with their performances (oldest first). Library
    view — feeds Browse's flat list, so this is a full-catalog dump.

    2026-08-24 (Ryan, "loads more quickly" / Browse's endless scroll): this
    used to run a query PER PERFORMER for performances, then lazy-load each
    performance's venue, performer (redundant — already have it as `pf`),
    and recordings, and each recording's tracks and quality_score — on a
    ~184-performer / ~580-recording library that's 2000+ separate DB round
    trips for one page load, which is what "loads all records at the
    outset" was actually slow at (the client-side row count was never the
    bottleneck; the fetch was). Rewritten as ONE root query with
    selectinload chains for every relationship walked below — SQLAlchemy
    batches each level into a single `WHERE ... IN (...)`, so this is now a
    small constant number of queries (one per relationship level) regardless
    of library size, not one per row. Sort order (was a SQL ORDER BY per
    performer) moves to Python since selectinload doesn't preserve a
    per-parent order across the whole call — same nullslast-ascending
    semantics, just computed once each on the already-fetched list.
    """
    performers = (
        db.session.query(Performer)
        .options(
            selectinload(Performer.genre),
            selectinload(Performer.performances).selectinload(Performance.venue),
            selectinload(Performer.performances)
                .selectinload(Performance.recordings)
                .selectinload(Recording.tracks),
            selectinload(Performer.performances)
                .selectinload(Performance.recordings)
                .selectinload(Recording.quality_score),
        )
        .order_by(func.coalesce(Performer.sort_name, Performer.name))
        .all()
    )

    # nullslast-ascending, same ordering the old per-performer SQL query
    # produced — a None sorts as "not less than any number", i.e. last.
    def _perf_sort_key(p):
        return (
            (p.start_year  is None, p.start_year  or 0),
            (p.start_month is None, p.start_month or 0),
            (p.start_day   is None, p.start_day   or 0),
        )

    result = []
    for pf in performers:
        performances = sorted(pf.performances, key=_perf_sort_key)
        if not performances:
            continue
        perf_list = []
        for p in performances:
            v = p.venue
            perf_list.append({
                "performance_id": p.id,
                # `pf.name`, not `p.performer.name` — p.performer_id == pf.id
                # by construction (this is pf's own performances list), so
                # it's the same value without a second relationship walk.
                "performer_name": pf.name,
                "title":          p.title,
                "start_year":     p.start_year,
                "start_month":    p.start_month,
                "start_day":      p.start_day,
                "venue_name":     v.name    if v else None,
                "city":           v.city    if v else p.city,
                "state":          v.state   if v else p.state,
                "country":        v.country if v else p.country,
                "recordings":     [recording_summary(r) for r in p.recordings],
            })
        # Genre rides along per PERFORMER, not per recording — the model is
        # one genre per act (see the Genre dimension work, 2026-08-02). Added
        # 2026-08-23 for Browse's genre filter and the colour spine on every
        # row: without it the Library view would need a second request just to
        # colour a list it already has.
        g = pf.genre
        result.append({
            "performer_id":      pf.id,
            "performer_name":    pf.name,
            "genre":             g.name  if g else None,
            "genre_color":       g.color if g else None,
            "performance_count": len(perf_list),
            "recording_count":   sum(len(p["recordings"]) for p in perf_list),
            "performances":      perf_list,
        })
    return jsonify(result)


@bp.route("/<int:performer_id>")
@login_required
def get_performer(performer_id):
    p = db.session.get(Performer, performer_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id":        p.id,
        "name":      p.name,
        "sort_name": p.sort_name,
        "bio":       p.bio,
        "default_personnel_mode": p.default_personnel_mode,
        # Each entry still has {id, name} (existing frontend code reading
        # just those two keys keeps working unchanged) plus a new `stints`
        # list the Performer page's stint editor uses.
        "members":   _serialize_roster(p),
        "resources": [{"id": r.id, "label": r.label, "url": r.url} for r in p.resources],
        # Multi-image as of 2026-08-07. `has_image` is retained (existing
        # callers read it) but now derives from the images relationship, not
        # the legacy image_ext column.
        "has_image": bool(p.images),
        "images":    [_image_payload(i) for i in p.images],
        "dossier":   json.loads(p.dossier_json) if p.dossier_json else None,
        # Genre (2026-08-02) — a proper dimension, one FK, nullable. null
        # until Ryan assigns one by hand (no AI suggestion for this field).
        # `color` (2026-08-07) drives the Browse cards' colour flair.
        "genre":     {"id": p.genre.id, "name": p.genre.name,
                      "color": p.genre.color} if p.genre else None,
        # MusicBrainz facts (2026-08-07). `status` is sent even when null so
        # the page can distinguish "never looked up" from "looked, found
        # nothing" — only the former and 'ambiguous' should offer a Match
        # prompt.
        "musicbrainz": {
            "mbid":           p.mbid,
            "status":         p.mb_status,
            "type":           p.mb_type,
            "area":           p.mb_area,
            "begin":          p.mb_begin,
            "end":            p.mb_end,
            "disambiguation": p.mb_disambiguation,
            "links":          json.loads(p.mb_links_json) if p.mb_links_json else {},
            # Related acts — a list, so JSON rather than a column. Aliases,
            # tags and gender were dropped 2026-08-07 (the panel shows links
            # only); the default keeps the shape stable for rows written
            # before that.
            **(json.loads(p.mb_extra_json) if p.mb_extra_json else {"related": []}),
            "checked_at":     p.mb_checked_at.isoformat() if p.mb_checked_at else None,
        },
    })


@bp.route("/<int:performer_id>/recordings")
@login_required
def get_performer_recordings(performer_id):
    p = db.session.get(Performer, performer_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    performances = (
        db.session.query(Performance)
        .filter(Performance.performer_id == performer_id)
        .order_by(
            Performance.start_year.desc().nullsfirst(),
            Performance.start_month.desc().nullsfirst(),
            Performance.start_day.desc().nullsfirst(),
        ).all()
    )
    out = []
    for perf in performances:
        v = perf.venue
        out.append({
            "performance_id": perf.id,
            "performer_name": perf.performer.name,
            "title":          perf.title,
            "stage":          perf.stage,
            "start_year":     perf.start_year,
            "start_month":    perf.start_month,
            "start_day":      perf.start_day,
            "end_year":       perf.end_year,
            "end_month":      perf.end_month,
            "end_day":        perf.end_day,
            "venue_name":     v.name    if v else None,
            "city":           v.city    if v else perf.city,
            "state":          v.state   if v else perf.state,
            "country":        v.country if v else perf.country,
            "recordings":     [recording_summary(r) for r in perf.recordings],
        })
    return jsonify(out)


@bp.route("/", methods=["POST"])
@login_required
def create_performer():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if db.session.query(Performer).filter(func.lower(Performer.name) == name.lower()).first():
        return jsonify({"error": "Performer already exists"}), 409
    p = Performer(name=name, sort_name=data.get("sort_name"), bio=data.get("bio"))
    db.session.add(p)
    db.session.flush()
    # Artists are optional — only set members if the caller supplied any.
    if data.get("members"):
        set_performer_members(p, data["members"])
    # Same MusicBrainz lookup the ingest path runs — one code path, so a
    # manually added act is never a second-class citizen with an empty
    # Overview tab. Adds ~2s to this one click and cannot fail the create.
    musicbrainz.try_match_performer(p)
    db.session.commit()
    return jsonify({"id": p.id, "name": p.name, "mb_status": p.mb_status}), 201


@bp.route("/<int:performer_id>", methods=["PUT"])
@login_required
def update_performer(performer_id):
    p = db.session.get(Performer, performer_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json()
    if "default_personnel_mode" in data and data["default_personnel_mode"] not in ("inherit", "explicit"):
        return jsonify({"error": "default_personnel_mode must be 'inherit' or 'explicit'"}), 400
    if "genre_id" in data:
        gid = data["genre_id"]
        if gid is not None and not db.session.get(Genre, gid):
            return jsonify({"error": "genre not found"}), 400
        p.genre_id = gid
    for f in ["name", "sort_name", "bio", "default_personnel_mode"]:
        if f in data:
            setattr(p, f, data[f])
    if data.get("members") is not None:
        set_performer_members(p, data["members"])
    if data.get("resources") is not None:
        _set_resources(p, data["resources"])
    db.session.commit()
    return jsonify({"id": p.id})


@bp.route("/ai-estimate")
@login_required
def ai_estimate():
    """
    Rough cost of one AI Assist pass, for the current user's chosen model.

    A RANGE, not a figure: cost tracks how many web searches the model decides
    it needs, which isn't knowable up front. Quoting a single number would be a
    promise we can't keep — see estimate_cost_cents().
    """
    from app.utils.ai_assist import estimate_cost_cents
    model = get_pref(current_user.id, "ai_model") or "claude-sonnet-5"
    est = estimate_cost_cents(model)
    if not est:
        return jsonify({"model": model, "low_cents": None, "high_cents": None})
    return jsonify({"model": model, "low_cents": est[0], "high_cents": est[1]})


# ── MusicBrainz match resolution (2026-08-07) ────────────────────────────────
# Automatic matching runs at Performer creation and either succeeds outright or
# records `mb_status='ambiguous'`. It NEVER auto-picks a top match (Ryan's
# call), so these endpoints are how a human finishes the job.

@bp.route("/<int:performer_id>/musicbrainz/lookup", methods=["POST"])
@login_required
def musicbrainz_lookup(performer_id):
    """
    Run a lookup and LINK IT IF THE ANSWER IS OBVIOUS.

    Ryan, 2026-08-07: making someone click through a candidate list to confirm a
    single 100-scoring result is busywork — if it's obvious, just do it. So this
    applies the same confidence gate as the creation-time pass. Only a genuinely
    unclear result hands back candidates for a human to choose from, and that
    choice is recorded as 'linked' rather than 'matched' so the page never
    claims credit for work a person did.
    """
    p = db.session.get(Performer, performer_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    if not musicbrainz.enabled():
        return jsonify({"error": "MusicBrainz lookups are disabled"}), 503

    # An explicit user action is exactly the "try again" the breaker waits for.
    musicbrainz.reset_breaker()
    term = (request.json or {}).get("q") if request.is_json else None
    term = (term or p.name).strip()

    candidates = musicbrainz.search_artist(term, limit=8)
    status, best = musicbrainz.classify(candidates)

    if status == "matched":
        details = musicbrainz.lookup_details(best["mbid"]) or best
        musicbrainz.apply_to_performer(p, details, details.get("links"),
                                       status="matched")
        db.session.commit()
        return jsonify({"status": "matched", "auto": True})

    p.mb_status = status                    # 'ambiguous' or 'none'
    p.mb_checked_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"status": status, "auto": False,
                    "query": term, "candidates": candidates})


@bp.route("/<int:performer_id>/musicbrainz/candidates", methods=["GET"])
@login_required
def musicbrainz_candidates(performer_id):
    """
    Candidate matches for this performer, best first.

    Live search rather than something cached at creation time: MusicBrainz gains
    entries constantly, and an act that had no match in July may well have one
    now. `?q=` overrides the search term so a user can correct a name that was
    misparsed from an info file without renaming the Performer first.
    """
    p = db.session.get(Performer, performer_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    if not musicbrainz.enabled():
        return jsonify({"error": "MusicBrainz lookups are disabled"}), 503

    # An explicit user action is exactly the "try again" the breaker waits for.
    musicbrainz.reset_breaker()
    term = (request.args.get("q") or p.name).strip()
    return jsonify({"query": term, "candidates": musicbrainz.search_artist(term, limit=8)})


@bp.route("/<int:performer_id>/musicbrainz", methods=["POST"])
@login_required
def musicbrainz_resolve(performer_id):
    """
    Attach a chosen MBID to this performer, or clear the association.

    POST {"mbid": "..."} to set, {"mbid": null} to clear back to unmatched.
    Clearing matters: a wrong match must be undoable without deleting the act.
    """
    p = db.session.get(Performer, performer_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    mbid = (data.get("mbid") or "").strip() or None

    if mbid is None:
        for attr in ("mbid", "mb_type", "mb_area", "mb_begin", "mb_end",
                     "mb_disambiguation", "mb_links_json"):
            setattr(p, attr, None)
        p.mb_status = None          # back to "never looked up", so it retries
        p.mb_checked_at = None
        db.session.commit()
        return jsonify({"status": None})

    musicbrainz.reset_breaker()
    details = musicbrainz.lookup_details(mbid)
    if not details:
        return jsonify({"error": "Could not fetch that MusicBrainz entry"}), 502
    # status='linked': a human picked this from the list. The page says
    # "Linked by you" rather than claiming an automatic match it didn't make.
    musicbrainz.apply_to_performer(p, details, details.get("links"), status="linked")
    db.session.commit()
    return jsonify({"status": "linked", "mbid": p.mbid,
                    "type": p.mb_type, "area": p.mb_area,
                    "begin": p.mb_begin, "end": p.mb_end,
                    "members": details.get("members") or []})


@bp.route("/<int:performer_id>/musicbrainz/members", methods=["GET"])
@login_required
def musicbrainz_members(performer_id):
    """
    Band members from MusicBrainz — FOR DISPLAY ONLY.

    Nothing here writes Membership rows. MusicBrainz's membership data maps
    almost exactly onto our stints model, and that is precisely why it stays
    read-only: roster changes cascade into per-show personnel resolution, and a
    silent write there is the failure mode fixed in July. The UI offers an
    explicit per-person Add.
    """
    p = db.session.get(Performer, performer_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    if not p.mbid:
        return jsonify({"members": []})
    details = musicbrainz.lookup_details(p.mbid)
    return jsonify({"members": (details or {}).get("members") or []})


# ── Profile pictures (2026-07-22; multi-image 2026-08-07) ────────────────────
# Files live on disk (never in the DB) at
# LIBRARY_ROOT/<sanitized name>/_images/<filename> — see
# _performer_images_dir() and app/models/performer_image.py.
#
# A Performer may hold many images, exactly one flagged primary. The primary is
# the face on Browse cards; the rest are simply available. `performer.image_ext`
# is LEGACY and read by nothing — the migration backfilled it into a row.
#
# Route shape note: the old singular `/image` endpoints are GONE rather than
# kept as aliases. An alias would have to invent "which image does the singular
# route mean on write?", and every answer is a silent surprise once a performer
# has several.

_IMG_URL = "/api/performers/images"


def _image_payload(img):
    """Thin alias — the shape lives in utils/entity_images so both image tables
    serialize identically."""
    return ei.image_payload(img, _IMG_URL)


@bp.route("/<int:performer_id>/images", methods=["GET"])
@login_required
def list_performer_images(performer_id):
    p = db.session.get(Performer, performer_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    return jsonify([_image_payload(i) for i in p.images])


@bp.route("/<int:performer_id>/images", methods=["POST"])
@login_required
def upload_performer_images(performer_id):
    """
    Upload one or more images. Accepts repeated `image` parts so drag-and-drop
    can send a whole dropped selection in a single request.

    Body lives in app/utils/entity_images.py (2026-08-07) so Performer and Venue
    photo management cannot drift apart in behaviour — first-image-becomes-
    primary, partial-success reporting and random basenames are all decided
    there, once.
    """
    p = db.session.get(Performer, performer_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    return ei.handle_upload(p, PerformerImage, _performer_images_dir(p), _IMG_URL)


@bp.route("/<int:performer_id>/images/fetch", methods=["POST"])
@login_required
def fetch_performer_image(performer_id):
    """
    Fetch a freely-licensed photo via Wikidata → Wikimedia Commons.

    Requires a MusicBrainz match first — the Wikidata link comes from there, so
    an unmatched performer has nothing to follow. That dependency is the reason
    the Photos tab points people at MusicBrainz when this is unavailable.

    Only Commons is used, never Wikipedia's local file namespace: Wikipedia
    hosts non-free "fair use" images that look identical but cannot be
    redistributed. Anything stored here carries a licence and an attribution
    line in `credit`; an image whose licence can't be read is refused rather
    than saved with a guess.

    Becomes primary ONLY if the performer has no photos yet — the same rule
    uploads follow. A photo you chose is never displaced by a fetched one.
    """
    p = db.session.get(Performer, performer_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    if not p.mbid:
        return jsonify({"error": "Match this act on MusicBrainz first — "
                                 "the photo lookup follows its Wikidata link."}), 400

    # Skip Commons files already imported for this act, so clicking a second
    # time returns a DIFFERENT photo rather than the same one again.
    already = {i.source_ref for i in p.images if i.source_ref}
    found = commons.find_photo_for_performer(p, exclude=already)
    if not found:
        # Not an error: most acts genuinely have no freely-licensed photo, and
        # on a repeat click "no MORE photos" is the norm — an act with two free
        # images is unusual. `had_any` lets the UI word those two cases
        # differently instead of saying "none found" when one is on screen.
        return jsonify({"found": False, "had_any": bool(p.images)}), 200

    images_dir = _performer_images_dir(p)
    images_dir.mkdir(parents=True, exist_ok=True)
    fname = f"img_{secrets.token_hex(6)}{found['ext']}"
    (images_dir / fname).write_bytes(found["data"])

    had_any = bool(p.images)
    img = PerformerImage(
        performer_id=p.id, filename=fname, ext=found["ext"],
        origin="commons", credit=found["credit"], caption=found.get("caption"),
        source_ref=found.get("source_ref"),
        sort_order=(max((i.sort_order for i in p.images), default=-1)) + 1,
    )
    db.session.add(img)
    db.session.flush()
    if not had_any:
        set_primary(img)
    db.session.commit()
    return jsonify({"found": True, "image": _image_payload(img),
                    "source_url": found.get("source_url")})


@bp.route("/images/<int:image_id>", methods=["GET"])
@login_required
@require_library(kind="image")
def serve_performer_image(image_id):
    """Serve one image by its own id — keyed on the image rather than the
    performer, so a card can request exactly the photo the serializer named."""
    img = db.session.get(PerformerImage, image_id)
    if not img:
        return jsonify({"error": "No image"}), 404
    return ei.handle_serve(img, _performer_images_dir(img.performer))


@bp.route("/images/<int:image_id>/primary", methods=["POST"])
@login_required
def make_performer_image_primary(image_id):
    img = db.session.get(PerformerImage, image_id)
    if not img:
        return jsonify({"error": "Not found"}), 404
    ei.set_primary(img)
    db.session.commit()
    return jsonify(ei.image_payload(img, _IMG_URL))


@bp.route("/images/<int:image_id>", methods=["PUT"])
@login_required
def update_performer_image(image_id):
    """Edit caption/credit. Credit matters for fetched images — a CC-licensed
    Commons photo carries an attribution requirement."""
    img = db.session.get(PerformerImage, image_id)
    if not img:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    if "caption" in data:
        img.caption = (data["caption"] or "").strip() or None
    if "credit" in data:
        img.credit = (data["credit"] or "").strip() or None
    db.session.commit()
    return jsonify(ei.image_payload(img, _IMG_URL))


@bp.route("/images/<int:image_id>", methods=["DELETE"])
@login_required
def delete_performer_image(image_id):
    img = db.session.get(PerformerImage, image_id)
    if not img:
        return jsonify({"error": "Not found"}), 404
    return ei.handle_delete(img, _performer_images_dir(img.performer))


# ── Dossier — AI-drafted biography + suggested resource links (2026-07-22) ──
# Background job, same shape as ingest.py's AI Assist (_AI_JOBS / poll):
# the synchronous Anthropic call is too slow for the webview's fetch timeout,
# so this starts a daemon thread and the client polls for the result. On
# success the raw result is persisted to Performer.dossier_json — nothing
# else is auto-applied (see performer_research.py's module docstring).
_DOSSIER_JOBS = {}  # job_id -> {"status": running|done|error, "result"/"error"}


def _run_dossier_job(job_id, performer_id, performer_name, current_bio, api_key, model, app):
    import traceback as _tb
    try:
        result = run_performer_research(
            performer_name, current_bio, api_key, model)
        _DOSSIER_JOBS[job_id] = {"status": "done", "result": result}
        try:
            with app.app_context():
                p = db.session.get(Performer, performer_id)
                if p:
                    p.dossier_json = json.dumps(result)
                    db.session.commit()
        except Exception:
            _tb.print_exc()   # best-effort — client already has the result via the job dict
    except AiAssistError as e:
        _DOSSIER_JOBS[job_id] = {"status": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        _tb.print_exc()
        _DOSSIER_JOBS[job_id] = {"status": "error", "error": "Unexpected error: %s" % e}


@bp.route("/<int:performer_id>/dossier", methods=["POST"])
@login_required
def start_dossier(performer_id):
    import threading
    import uuid

    p = db.session.get(Performer, performer_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    api_key = get_api_key(current_user.id)
    if not api_key:
        return jsonify({"error": "no_api_key"}), 428
    model = get_pref(current_user.id, "ai_model") or "claude-sonnet-5"

    job_id = uuid.uuid4().hex
    _DOSSIER_JOBS[job_id] = {"status": "running"}
    threading.Thread(
        target=_run_dossier_job,
        args=(job_id, performer_id, p.name, p.bio or "", api_key, model, current_app._get_current_object()),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id}), 202


@bp.route("/<int:performer_id>/dossier/<job_id>", methods=["GET"])
@login_required
def dossier_status(performer_id, job_id):
    job = _DOSSIER_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if job["status"] == "running":
        return jsonify({"status": "running"})
    _DOSSIER_JOBS.pop(job_id, None)   # deliver terminal state once, then discard
    if job["status"] == "error":
        return jsonify({"status": "error", "error": job["error"]})
    return jsonify({"status": "done", "result": job["result"]})


@bp.route("/<int:performer_id>/members/<int:artist_id>/stints", methods=["POST"])
@login_required
def add_stint(performer_id, artist_id):
    """
    Add a NEW stint for an existing member — how a second tenure (Mickey
    Hart's post-1974 return) gets recorded without touching the first. Does
    NOT create the membership from scratch if none exists yet; use the
    plain roster (PUT .../members) to add someone for the first time.
    """
    performer = db.session.get(Performer, performer_id)
    if not performer:
        return jsonify({"error": "Performer not found"}), 404
    artist = db.session.get(Artist, artist_id)
    if not artist:
        return jsonify({"error": "Artist not found"}), 404
    data = request.get_json() or {}
    m = add_membership_stint(
        performer, artist.name,
        start_year=data.get("start_year"), start_month=data.get("start_month"),
        start_day=data.get("start_day"),
        end_year=data.get("end_year"), end_month=data.get("end_month"),
        end_day=data.get("end_day"),
    )
    db.session.commit()
    return jsonify({"id": m.id}), 201


@bp.route("/stints/<int:stint_id>", methods=["PUT"])
@login_required
def update_stint(stint_id):
    """Edit one stint's date bounds. Does not affect a person's other stints."""
    data = request.get_json() or {}
    m = update_membership_stint_bounds(
        stint_id,
        start_year=data.get("start_year"), start_month=data.get("start_month"),
        start_day=data.get("start_day"),
        end_year=data.get("end_year"), end_month=data.get("end_month"),
        end_day=data.get("end_day"),
    )
    if not m:
        return jsonify({"error": "Not found"}), 404
    db.session.commit()
    return jsonify({"id": m.id})


@bp.route("/stints/<int:stint_id>", methods=["DELETE"])
@login_required
def delete_stint(stint_id):
    """
    Remove one stint row. Refuses if it's the member's ONLY stint — dropping
    someone to zero stints via a raw delete here would leave them dangling
    in a different way than the roster-remove path (set_performer_members'
    drop-a-name flow, which goes through its own orphan/prune-safe logic).
    To remove someone entirely, drop them from the plain roster instead.
    """
    m = db.session.get(Membership, stint_id)
    if not m:
        return jsonify({"error": "Not found"}), 404
    remaining = db.session.query(Membership).filter_by(
        performer_id=m.performer_id, artist_id=m.artist_id).count()
    if remaining <= 1:
        return jsonify({"error": "This is the member's only stint — remove them from "
                                 "the roster instead of deleting their last stint."}), 409
    remove_membership_stint(stint_id)
    db.session.commit()
    return jsonify({"ok": True})


def _set_resources(performer, resources):
    """Replace a performer's reference resources with the given ordered list of
    {label, url} dicts (rows with a blank url are skipped)."""
    db.session.query(PerformerResource).filter_by(performer_id=performer.id).delete(
        synchronize_session=False)
    db.session.flush()
    for i, r in enumerate(resources or []):
        url = (r.get("url") or "").strip()
        if not url:
            continue
        db.session.add(PerformerResource(
            performer_id=performer.id, url=url,
            label=(r.get("label") or "").strip() or None, order=i))
    db.session.flush()


@bp.route("/<int:performer_id>", methods=["DELETE"])
@login_required
def delete_performer(performer_id):
    """Delete a performer. Refuses if it still has performances/recordings —
    reassign or delete those first. Member Artists left orphaned are pruned."""
    p = db.session.get(Performer, performer_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    n_perf = db.session.query(Performance).filter_by(performer_id=performer_id).count()
    if n_perf:
        return jsonify({"error": f"Performer has {n_perf} performance(s) — "
                                 "delete or reassign its recordings first."}), 409
    member_ids = [a.id for a in p.artists]
    db.session.delete(p)          # memberships cascade
    db.session.flush()
    # Prune any member Artist that now belongs to no performer.
    for aid in member_ids:
        a = db.session.get(Artist, aid)
        if a and not a.memberships:
            db.session.delete(a)
    db.session.commit()
    return jsonify({"ok": True})
