"""
api/artists.py — Artist (act) endpoints: browse, catalog, search, members.

An Artist is the act you browse and tag by. Its member Musicians (people) are
managed here too. Grouping "everything by a person" lives on the Musician side
(api/musicians.py), not here.
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
from app.models.artist import Artist, ArtistResource
from app.models.artist_image import ArtistImage
from app.utils.entity_images import set_primary
from app.models.genre import Genre
from app.models.musician import Musician, Membership
from app.models.performance import Performance
from app.models.recording import Recording
from app.utils.serialize import recording_summary
from app.utils.ingest import _sanitize_path
from app.utils.artists import (
    set_artist_members, add_membership_stint,
    update_membership_stint_bounds, remove_membership_stint,
)
from app.utils import musicbrainz, commons
from app.utils import entity_images as ei
from app.utils.artist_research import run_artist_research
from app.utils.ai_assist import AiAssistError
from app.utils.prefs import get_api_key, get_pref
from app.api.system import require_library

bp = Blueprint("artists", __name__)

# Canonical list lives in utils/entity_images so both image tables accept the
# same formats. Aliased here for the Commons fetch path below.
_ALLOWED_IMAGE_EXTS = ei.ALLOWED_IMAGE_EXTS


def _artist_images_dir(artist):
    """
    LIBRARY_ROOT/<sanitized name>/_images — the leading underscore sorts it
    first alongside/before recording folders in a Finder listing (Ryan,
    2026-07-22). NOTE: derived from the Artist's CURRENT name at request
    time, not a stored path — see Artist.image_ext's docstring for the
    rename-orphan caveat this carries (matches how existing recording
    folders already behave on a rename: nothing moves those either).
    """
    library_root = current_app.config["LIBRARY_ROOT"]
    return Path(library_root) / _sanitize_path(artist.name) / "_images"


def _serialize_roster(artist):
    """
    Member Musicians deduped by person (see Artist.musicians), each carrying
    their stint row(s) — usually one unbounded row ('always a member'), but
    possibly several for someone with real tenure gaps (Mickey Hart). Powers
    the Artist page's stint editor.
    """
    by_musician = {}
    for m in artist.memberships:   # already ordered by Membership.order
        by_musician.setdefault(m.musician_id, []).append(m)
    roster = []
    for musician_id, stints in by_musician.items():
        roster.append({
            "id":   musician_id,
            "name": stints[0].musician.name,
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
def search_artists():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    rows = (db.session.query(Artist)
            .filter(Artist.name.ilike(f"%{q}%"))
            .order_by(Artist.name).limit(12).all())
    return jsonify([{"id": p.id, "name": p.name} for p in rows])


@bp.route("/")
@login_required
def list_artists():
    """All artists with recording counts + member names — powers the sidebar."""
    rows = (
        db.session.query(Artist, func.count(Recording.id).label("rc"))
        .outerjoin(Performance, Performance.artist_id == Artist.id)
        .outerjoin(Recording,   Recording.performance_id == Performance.id)
        .group_by(Artist.id)
        .order_by(func.coalesce(Artist.sort_name, Artist.name))
        .all()
    )
    # Primary photo id per artist, for the Artists index tiles
    # (2026-09-01). One grouped query, not `p.images[0]` per row — this endpoint
    # is called on every sidebar render, and on a 184-artist library that
    # would be 184 extra round trips to draw a nav list that shows no photos at
    # all. Primary first, oldest as the fallback, matching primary_for().
    image_ids = {}
    for pid, iid, _pr in (
        db.session.query(ArtistImage.artist_id, ArtistImage.id,
                         ArtistImage.is_primary)
        .order_by(ArtistImage.artist_id, ArtistImage.is_primary.desc(),
                  ArtistImage.sort_order, ArtistImage.id).all()
    ):
        image_ids.setdefault(pid, iid)

    return jsonify([
        {
            "id":              p.id,
            "name":            p.name,
            "sort_name":       p.sort_name,
            "recording_count": rc,
            "members":         [a.name for a in p.musicians],
            # Genre (2026-08-02) — id + name only, powers the bulk assignment
            # screen's "unassigned only" filter and pre-fill.
            "genre_id":        p.genre_id,
            "genre_name":      p.genre.name if p.genre else None,
            # Colour added 2026-09-01 for the index tiles. May be None even when
            # a genre exists — colour is nullable and NULL is a supported state
            # that renders neutral. Never substitute a default here; the
            # fallback belongs in one place, in the frontend.
            "genre_color":     p.genre.color if p.genre else None,
            "image_id":        image_ids.get(p.id),
        }
        for p, rc in rows
    ])


@bp.route("/all-recordings")
@login_required
def all_recordings():
    """
    Every artist (alpha) with their performances (oldest first). Library
    view — feeds Browse's flat list, so this is a full-catalog dump.

    2026-08-24 (Ryan, "loads more quickly" / Browse's endless scroll): this
    used to run a query PER ARTIST for performances, then lazy-load each
    performance's venue, artist (redundant — already have it as `pf`),
    and recordings, and each recording's tracks and quality_score — on a
    ~184-artist / ~580-recording library that's 2000+ separate DB round
    trips for one page load, which is what "loads all records at the
    outset" was actually slow at (the client-side row count was never the
    bottleneck; the fetch was). Rewritten as ONE root query with
    selectinload chains for every relationship walked below — SQLAlchemy
    batches each level into a single `WHERE ... IN (...)`, so this is now a
    small constant number of queries (one per relationship level) regardless
    of library size, not one per row. Sort order (was a SQL ORDER BY per
    artist) moves to Python since selectinload doesn't preserve a
    per-parent order across the whole call — same nullslast-ascending
    semantics, just computed once each on the already-fetched list.
    """
    # Primary photo id per artist, for Browse's row avatars (2026-09-02).
    # ONE grouped query, deliberately, not `selectinload(Artist.images)` —
    # this endpoint is the full-catalog dump and eager-loading every image row
    # for 184 artists to read one id off each is the same shape of waste the
    # 2026-08-24 rewrite existed to remove. Primary first, oldest as the
    # fallback, matching `primary_for()` and `list_artists()` above.
    image_ids = {}
    for pid, iid, _pr in (
        db.session.query(ArtistImage.artist_id, ArtistImage.id,
                         ArtistImage.is_primary)
        .order_by(ArtistImage.artist_id, ArtistImage.is_primary.desc(),
                  ArtistImage.sort_order, ArtistImage.id).all()
    ):
        image_ids.setdefault(pid, iid)

    artists = (
        db.session.query(Artist)
        .options(
            selectinload(Artist.genre),
            selectinload(Artist.performances).selectinload(Performance.venue),
            selectinload(Artist.performances)
                .selectinload(Performance.recordings)
                .selectinload(Recording.tracks),
            selectinload(Artist.performances)
                .selectinload(Performance.recordings)
                .selectinload(Recording.quality_score),
        )
        .order_by(func.coalesce(Artist.sort_name, Artist.name))
        .all()
    )

    # nullslast-ascending, same ordering the old per-artist SQL query
    # produced — a None sorts as "not less than any number", i.e. last.
    def _perf_sort_key(p):
        return (
            (p.start_year  is None, p.start_year  or 0),
            (p.start_month is None, p.start_month or 0),
            (p.start_day   is None, p.start_day   or 0),
        )

    result = []
    for pf in artists:
        performances = sorted(pf.performances, key=_perf_sort_key)
        if not performances:
            continue
        perf_list = []
        for p in performances:
            v = p.venue
            perf_list.append({
                "performance_id": p.id,
                # `pf.name`, not `p.artist.name` — p.artist_id == pf.id
                # by construction (this is pf's own performances list), so
                # it's the same value without a second relationship walk.
                "artist_name": pf.name,
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
        # Genre rides along per ARTIST, not per recording — the model is
        # one genre per act (see the Genre dimension work, 2026-08-02). Added
        # 2026-08-23 for Browse's genre filter and the colour spine on every
        # row: without it the Library view would need a second request just to
        # colour a list it already has.
        g = pf.genre
        result.append({
            "artist_id":      pf.id,
            "artist_name":    pf.name,
            "genre":             g.name  if g else None,
            "genre_color":       g.color if g else None,
            # Browse's flat list draws a photo where there is one and the
            # genre-coloured initials square where there is not (2026-09-02).
            # It had only ever had the initials — not because the photos were
            # missing (103 of 184 artists have one) but because this payload
            # never carried the id to fetch them with.
            "image_id":          image_ids.get(pf.id),
            "performance_count": len(perf_list),
            "recording_count":   sum(len(p["recordings"]) for p in perf_list),
            "performances":      perf_list,
        })
    return jsonify(result)


@bp.route("/<int:artist_id>")
@login_required
def get_artist(artist_id):
    p = db.session.get(Artist, artist_id)
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
        # list the Artist page's stint editor uses.
        "members":   _serialize_roster(p),
        "resources": [{"id": r.id, "label": r.label, "url": r.url} for r in p.resources],
        # Multi-image as of 2026-08-07. `has_image` is retained (existing
        # callers read it) but now derives from the images relationship, not
        # the legacy image_ext column.
        "has_image": bool(p.images),
        "images":    [_image_payload(i) for i in p.images],
        "dossier":   json.loads(p.dossier_json) if p.dossier_json else None,
        # The last lineup-research pass, so it survives navigating away
        # (Ryan, 2026-09-07). Deliberately NOT added to app/api/share.py: a
        # roster proposal awaiting review is the owner's working state, not
        # catalog metadata a peer has any use for — and every field added to
        # the peer surface is another way THE THREE LISTS drift apart.
        "lineup":    json.loads(p.lineup_json) if p.lineup_json else None,
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


@bp.route("/<int:artist_id>/recordings")
@login_required
def get_artist_recordings(artist_id):
    p = db.session.get(Artist, artist_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    performances = (
        db.session.query(Performance)
        .filter(Performance.artist_id == artist_id)
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
            "artist_name": perf.artist.name,
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
def create_artist():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if db.session.query(Artist).filter(func.lower(Artist.name) == name.lower()).first():
        return jsonify({"error": "Artist already exists"}), 409
    p = Artist(name=name, sort_name=data.get("sort_name"), bio=data.get("bio"))
    db.session.add(p)
    db.session.flush()
    # Musicians are optional — only set members if the caller supplied any.
    if data.get("members"):
        set_artist_members(p, data["members"])
    # Same MusicBrainz lookup the ingest path runs — one code path, so a
    # manually added act is never a second-class citizen with an empty
    # Overview tab. Adds ~2s to this one click and cannot fail the create.
    musicbrainz.try_match_artist(p)
    db.session.commit()
    return jsonify({"id": p.id, "name": p.name, "mb_status": p.mb_status}), 201


@bp.route("/<int:artist_id>", methods=["PUT"])
@login_required
def update_artist(artist_id):
    p = db.session.get(Artist, artist_id)
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
        set_artist_members(p, data["members"])
    if data.get("resources") is not None:
        _set_resources(p, data["resources"])
    db.session.commit()
    return jsonify({"id": p.id})


@bp.route("/ai-estimate")
@login_required
def ai_estimate():
    """
    Rough TOKEN cost of one AI Assist pass. No currency figure — see
    ai_assist.py's usage-reporting comment for why that was removed.

    A RANGE, not a figure: usage tracks how many web searches the model decides
    it needs, which isn't knowable up front. Quoting a single number would be a
    promise we can't keep. No longer takes the model into account, because a
    token count is a property of the work rather than of who is billed.
    """
    from app.utils.ai_assist import estimate_tokens, MAX_SEARCHES
    low, high = estimate_tokens()
    return jsonify({"low_tokens": low, "high_tokens": high,
                    "max_searches": MAX_SEARCHES})


# ── MusicBrainz match resolution (2026-08-07) ────────────────────────────────
# Automatic matching runs at Artist creation and either succeeds outright or
# records `mb_status='ambiguous'`. It NEVER auto-picks a top match (Ryan's
# call), so these endpoints are how a human finishes the job.

@bp.route("/<int:artist_id>/musicbrainz/lookup", methods=["POST"])
@login_required
def musicbrainz_lookup(artist_id):
    """
    Run a lookup and LINK IT IF THE ANSWER IS OBVIOUS.

    Ryan, 2026-08-07: making someone click through a candidate list to confirm a
    single 100-scoring result is busywork — if it's obvious, just do it. So this
    applies the same confidence gate as the creation-time pass. Only a genuinely
    unclear result hands back candidates for a human to choose from, and that
    choice is recorded as 'linked' rather than 'matched' so the page never
    claims credit for work a person did.
    """
    p = db.session.get(Artist, artist_id)
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
        musicbrainz.apply_to_artist(p, details, details.get("links"),
                                       status="matched")
        db.session.commit()
        return jsonify({"status": "matched", "auto": True})

    p.mb_status = status                    # 'ambiguous' or 'none'
    p.mb_checked_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"status": status, "auto": False,
                    "query": term, "candidates": candidates})


@bp.route("/<int:artist_id>/musicbrainz/candidates", methods=["GET"])
@login_required
def musicbrainz_candidates(artist_id):
    """
    Candidate matches for this artist, best first.

    Live search rather than something cached at creation time: MusicBrainz gains
    entries constantly, and an act that had no match in July may well have one
    now. `?q=` overrides the search term so a user can correct a name that was
    misparsed from an info file without renaming the Artist first.
    """
    p = db.session.get(Artist, artist_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    if not musicbrainz.enabled():
        return jsonify({"error": "MusicBrainz lookups are disabled"}), 503

    # An explicit user action is exactly the "try again" the breaker waits for.
    musicbrainz.reset_breaker()
    term = (request.args.get("q") or p.name).strip()
    return jsonify({"query": term, "candidates": musicbrainz.search_artist(term, limit=8)})


@bp.route("/<int:artist_id>/musicbrainz", methods=["POST"])
@login_required
def musicbrainz_resolve(artist_id):
    """
    Attach a chosen MBID to this artist, or clear the association.

    POST {"mbid": "..."} to set, {"mbid": null} to clear back to unmatched.
    Clearing matters: a wrong match must be undoable without deleting the act.
    """
    p = db.session.get(Artist, artist_id)
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
    musicbrainz.apply_to_artist(p, details, details.get("links"), status="linked")
    db.session.commit()
    return jsonify({"status": "linked", "mbid": p.mbid,
                    "type": p.mb_type, "area": p.mb_area,
                    "begin": p.mb_begin, "end": p.mb_end,
                    "members": details.get("members") or []})


@bp.route("/<int:artist_id>/musicbrainz/members", methods=["GET"])
@login_required
def musicbrainz_members(artist_id):
    """
    Band members from MusicBrainz — FOR DISPLAY ONLY.

    Nothing here writes Membership rows. MusicBrainz's membership data maps
    almost exactly onto our stints model, and that is precisely why it stays
    read-only: roster changes cascade into per-show personnel resolution, and a
    silent write there is the failure mode fixed in July. The UI offers an
    explicit per-person Add.
    """
    p = db.session.get(Artist, artist_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    if not p.mbid:
        return jsonify({"members": []})
    details = musicbrainz.lookup_details(p.mbid)
    return jsonify({"members": (details or {}).get("members") or []})


# ── Profile pictures (2026-07-22; multi-image 2026-08-07) ────────────────────
# Files live on disk (never in the DB) at
# LIBRARY_ROOT/<sanitized name>/_images/<filename> — see
# _artist_images_dir() and app/models/artist_image.py.
#
# An Artist may hold many images, exactly one flagged primary. The primary is
# the face on Browse cards; the rest are simply available. `artist.image_ext`
# is LEGACY and read by nothing — the migration backfilled it into a row.
#
# Route shape note: the old singular `/image` endpoints are GONE rather than
# kept as aliases. An alias would have to invent "which image does the singular
# route mean on write?", and every answer is a silent surprise once an artist
# has several.

_IMG_URL = "/api/artists/images"


def _image_payload(img):
    """Thin alias — the shape lives in utils/entity_images so both image tables
    serialize identically."""
    return ei.image_payload(img, _IMG_URL)


@bp.route("/<int:artist_id>/images", methods=["GET"])
@login_required
def list_artist_images(artist_id):
    p = db.session.get(Artist, artist_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    return jsonify([_image_payload(i) for i in p.images])


@bp.route("/<int:artist_id>/images", methods=["POST"])
@login_required
def upload_artist_images(artist_id):
    """
    Upload one or more images. Accepts repeated `image` parts so drag-and-drop
    can send a whole dropped selection in a single request.

    Body lives in app/utils/entity_images.py (2026-08-07) so Artist and Venue
    photo management cannot drift apart in behaviour — first-image-becomes-
    primary, partial-success reporting and random basenames are all decided
    there, once.
    """
    p = db.session.get(Artist, artist_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    return ei.handle_upload(p, ArtistImage, _artist_images_dir(p), _IMG_URL)


@bp.route("/<int:artist_id>/images/fetch", methods=["POST"])
@login_required
def fetch_artist_image(artist_id):
    """
    Fetch a freely-licensed photo via Wikidata → Wikimedia Commons.

    Requires a MusicBrainz match first — the Wikidata link comes from there, so
    an unmatched artist has nothing to follow. That dependency is the reason
    the Photos tab points people at MusicBrainz when this is unavailable.

    Only Commons is used, never Wikipedia's local file namespace: Wikipedia
    hosts non-free "fair use" images that look identical but cannot be
    redistributed. Anything stored here carries a licence and an attribution
    line in `credit`; an image whose licence can't be read is refused rather
    than saved with a guess.

    Becomes primary ONLY if the artist has no photos yet — the same rule
    uploads follow. A photo you chose is never displaced by a fetched one.
    """
    p = db.session.get(Artist, artist_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    if not p.mbid:
        return jsonify({"error": "Match this act on MusicBrainz first — "
                                 "the photo lookup follows its Wikidata link."}), 400

    # Skip Commons files already imported for this act, so clicking a second
    # time returns a DIFFERENT photo rather than the same one again.
    already = {i.source_ref for i in p.images if i.source_ref}
    found = commons.find_photo_for_artist(p, exclude=already)
    if not found:
        # Not an error: most acts genuinely have no freely-licensed photo, and
        # on a repeat click "no MORE photos" is the norm — an act with two free
        # images is unusual. `had_any` lets the UI word those two cases
        # differently instead of saying "none found" when one is on screen.
        return jsonify({"found": False, "had_any": bool(p.images)}), 200

    images_dir = _artist_images_dir(p)
    images_dir.mkdir(parents=True, exist_ok=True)
    fname = f"img_{secrets.token_hex(6)}{found['ext']}"
    (images_dir / fname).write_bytes(found["data"])

    had_any = bool(p.images)
    img = ArtistImage(
        artist_id=p.id, filename=fname, ext=found["ext"],
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
def serve_artist_image(image_id):
    """Serve one image by its own id — keyed on the image rather than the
    artist, so a card can request exactly the photo the serializer named."""
    img = db.session.get(ArtistImage, image_id)
    if not img:
        return jsonify({"error": "No image"}), 404
    return ei.handle_serve(img, _artist_images_dir(img.artist))


@bp.route("/images/<int:image_id>/primary", methods=["POST"])
@login_required
def make_artist_image_primary(image_id):
    img = db.session.get(ArtistImage, image_id)
    if not img:
        return jsonify({"error": "Not found"}), 404
    ei.set_primary(img)
    db.session.commit()
    return jsonify(ei.image_payload(img, _IMG_URL))


@bp.route("/images/<int:image_id>", methods=["PUT"])
@login_required
def update_artist_image(image_id):
    """Edit caption/credit. Credit matters for fetched images — a CC-licensed
    Commons photo carries an attribution requirement."""
    img = db.session.get(ArtistImage, image_id)
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
def delete_artist_image(image_id):
    img = db.session.get(ArtistImage, image_id)
    if not img:
        return jsonify({"error": "Not found"}), 404
    return ei.handle_delete(img, _artist_images_dir(img.artist))


# ── Dossier — AI-drafted biography + suggested resource links (2026-07-22) ──
# Background job, same shape as ingest.py's AI Assist (_AI_JOBS / poll):
# the synchronous Anthropic call is too slow for the webview's fetch timeout,
# so this starts a daemon thread and the client polls for the result. On
# success the raw result is persisted to Artist.dossier_json — nothing
# else is auto-applied (see artist_research.py's module docstring).
_DOSSIER_JOBS = {}  # job_id -> {"status": running|done|error, "result"/"error"}


def _artist_context(p):
    """
    What the DB already knows about this act, handed to the model as ground
    truth so it does not spend searches re-deriving it (2026-09-07).

    MusicBrainz aliases matter more here than they look: same-name acts are the
    main way this research goes wrong, and origin + active years + aliases is
    usually what separates two of them.
    """
    extra = {}
    if p.mb_extra_json:
        try:
            extra = json.loads(p.mb_extra_json) or {}
        except (ValueError, TypeError):
            extra = {}
    return {
        "aliases":           [a for a in (extra.get("aliases") or []) if a][:8],
        "mb_type":           p.mb_type,
        "mb_area":           p.mb_area,
        "mb_begin":          p.mb_begin,
        "mb_end":            p.mb_end,
        "mb_disambiguation": p.mb_disambiguation,
        "genre":             p.genre.name if p.genre else None,
        "members":           [a.name for a in p.musicians],
    }


def _run_dossier_job(job_id, artist_id, artist_name, current_bio, api_key, model, app,
                     mode="bio", question=None, context=None):
    import traceback as _tb
    try:
        result = run_artist_research(
            artist_name, current_bio, api_key, model,
            context=context, question=question, mode=mode)
        _DOSSIER_JOBS[job_id] = {"status": "done", "result": result}
        try:
            with app.app_context():
                p = db.session.get(Artist, artist_id)
                # Each mode owns its own column. Sharing one would mean a
                # lineup run silently destroying the last biography research,
                # and vice versa.
                if p and mode == "bio":
                    p.dossier_json = json.dumps(result)
                    db.session.commit()
                elif p and mode == "lineup":
                    p.lineup_json = json.dumps(result)
                    db.session.commit()
        except Exception:
            _tb.print_exc()   # best-effort — client already has the result via the job dict
    except AiAssistError as e:
        _DOSSIER_JOBS[job_id] = {"status": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        _tb.print_exc()
        _DOSSIER_JOBS[job_id] = {"status": "error", "error": "Unexpected error: %s" % e}


@bp.route("/<int:artist_id>/dossier", methods=["POST"])
@login_required
def start_dossier(artist_id):
    import threading
    import uuid

    data = request.get_json(silent=True) or {}
    mode = "lineup" if data.get("mode") == "lineup" else "bio"

    p = db.session.get(Artist, artist_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    api_key = get_api_key(current_user.id)
    if not api_key:
        return jsonify({"error": "no_api_key"}), 428
    model = get_pref(current_user.id, "ai_model") or "claude-sonnet-5"

    context = _artist_context(p)
    job_id = uuid.uuid4().hex
    _DOSSIER_JOBS[job_id] = {"status": "running"}
    threading.Thread(
        target=_run_dossier_job,
        args=(job_id, artist_id, p.name, p.bio or "", api_key, model, current_app._get_current_object()),
        kwargs={"mode": mode, "question": data.get("question"), "context": context},
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id}), 202


@bp.route("/<int:artist_id>/dossier/<job_id>", methods=["GET"])
@login_required
def dossier_status(artist_id, job_id):
    job = _DOSSIER_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if job["status"] == "running":
        return jsonify({"status": "running"})
    _DOSSIER_JOBS.pop(job_id, None)   # deliver terminal state once, then discard
    if job["status"] == "error":
        return jsonify({"status": "error", "error": job["error"]})
    return jsonify({"status": "done", "result": job["result"]})


@bp.route("/<int:artist_id>/members/<int:musician_id>/stints", methods=["POST"])
@login_required
def add_stint(artist_id, musician_id):
    """
    Add a NEW stint for an existing member — how a second tenure (Mickey
    Hart's post-1974 return) gets recorded without touching the first. Does
    NOT create the membership from scratch if none exists yet; use the
    plain roster (PUT .../members) to add someone for the first time.
    """
    artist = db.session.get(Artist, artist_id)
    if not artist:
        return jsonify({"error": "Artist not found"}), 404
    musician = db.session.get(Musician, musician_id)
    if not musician:
        return jsonify({"error": "Musician not found"}), 404
    data = request.get_json() or {}
    m = add_membership_stint(
        artist, musician.name,
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
    in a different way than the roster-remove path (set_artist_members'
    drop-a-name flow, which goes through its own orphan/prune-safe logic).
    To remove someone entirely, drop them from the plain roster instead.
    """
    m = db.session.get(Membership, stint_id)
    if not m:
        return jsonify({"error": "Not found"}), 404
    remaining = db.session.query(Membership).filter_by(
        artist_id=m.artist_id, musician_id=m.musician_id).count()
    if remaining <= 1:
        return jsonify({"error": "This is the member's only stint — remove them from "
                                 "the roster instead of deleting their last stint."}), 409
    remove_membership_stint(stint_id)
    db.session.commit()
    return jsonify({"ok": True})


def _set_resources(artist, resources):
    """Replace an artist's reference resources with the given ordered list of
    {label, url} dicts (rows with a blank url are skipped)."""
    db.session.query(ArtistResource).filter_by(artist_id=artist.id).delete(
        synchronize_session=False)
    db.session.flush()
    for i, r in enumerate(resources or []):
        url = (r.get("url") or "").strip()
        if not url:
            continue
        db.session.add(ArtistResource(
            artist_id=artist.id, url=url,
            label=(r.get("label") or "").strip() or None, order=i))
    db.session.flush()


@bp.route("/<int:artist_id>", methods=["DELETE"])
@login_required
def delete_artist(artist_id):
    """Delete an artist. Refuses if it still has performances/recordings —
    reassign or delete those first. Member Musicians left orphaned are pruned."""
    p = db.session.get(Artist, artist_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    n_perf = db.session.query(Performance).filter_by(artist_id=artist_id).count()
    if n_perf:
        return jsonify({"error": f"Artist has {n_perf} performance(s) — "
                                 "delete or reassign its recordings first."}), 409
    member_ids = [a.id for a in p.musicians]
    db.session.delete(p)          # memberships cascade
    db.session.flush()
    # Prune any member Musician that now belongs to no artist.
    for aid in member_ids:
        a = db.session.get(Musician, aid)
        if a and not a.memberships:
            db.session.delete(a)
    db.session.commit()
    return jsonify({"ok": True})
