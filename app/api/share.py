"""
api/share.py — the peer-facing door (INBOUND sharing).

Everything here is authenticated by a peer Bearer token (@peer_required),
NOT a local login. This blueprint is deliberately READ-ONLY: it exposes
browse + stream over a peer's granted collections and nothing else. There is
no route here that mutates the library — a peer token is structurally
incapable of editing/deleting, because the only endpoints it can authenticate
to are the ones in this file. See "Peer Sharing — Design Spec v1".

Routes (url_prefix /api/share):
  POST /enroll                    invite-code handshake → mints a token (the
                                  only route NOT behind @peer_required; the
                                  invite code itself is the credential)
  GET  /me                        who am I / node + owner identity
  GET  /collections               collections granted to me
  GET  /collections/<id>          recordings in a granted collection
  GET  /recordings/<id>           full recording detail (read-only)
  GET  /stream/<track_id>         transcoded MP3 256k stream (access-checked)

Entity pages (milestone 2, 2026-08-08) — paths mirror the LOCAL API so the
consumer's frontend can reuse its existing render functions:
  GET  /performers/<id>              catalog metadata (bio, dossier, genre…)
  GET  /performers/<id>/recordings   holdings — filtered to the visible set
  GET  /performers/images/<image_id> performer photo, checked via its owner
  GET  /venues/<id>                  venue + its visible shows, visible counts
  GET  /artists/<id>                 person, visible acts + guest appearances
  GET  /genres/                      genres present in the visible set
  GET  /genres/<id>                  one genre, its visible acts and shows

All routes are strict_slashes=False (2026-08-08): the consumer proxies paths
built by the LOCAL api.js, whose conventions differ on trailing slashes
(/api/collections/ but /api/venues/12). A peer client should not have to guess.

Every one of those is filtered through peer_visible_recording_ids(). Counts
included — see the rule in the ENTITY PAGES banner below.
"""

from datetime import datetime, timezone
import json as _json

from flask import Blueprint, request, jsonify, g, current_app, abort, send_file

from app.extensions import db
from app.models.user import User
from app.models.peer import Peer, PeerInvite, PeerToken, PeerAccessLog
from app.models.collection import Collection
from app.models.recording import Recording
from app.models.track import Track
from app.utils.peer_auth import (
    peer_required, current_peer, hash_secret, generate_token,
)
from app.utils.peer_access import (
    peer_granted_collection_ids, peer_can_access_recording, peer_can_access_track,
    peer_visible_recording_ids, peer_visible_performance_ids,
    peer_visible_performer_ids, peer_visible_venue_ids, peer_visible_artist_ids,
    peer_can_access_performer, peer_can_access_venue,
    peer_can_access_artist,
)
from app.utils.serialize import recording_row, recording_summary
from app.utils.rate_limit import rate_limited
from app.utils.format import format_partial_date
from app.api.stream import _serve_file
from app.utils import transcode as tx

bp = Blueprint("share", __name__)


def _utcnow():
    return datetime.now(timezone.utc)


def _owner_user():
    """The person whose library this is: the first active admin."""
    return db.session.query(User).filter_by(role="admin", is_active=True).first()


def _node_identity():
    """This instance's public identity, shown to peers on enroll/‘me’.
    Config-driven with sensible fallbacks; SHARE_NODE_NAME / SHARE_OWNER_NAME
    become real settings in the server-mode config milestone."""
    # Owner first — the node name is derived from it when unset.
    owner = current_app.config.get("SHARE_OWNER_NAME")
    if not owner:
        admin = _owner_user()
        # `.name`, not `.username` (2026-08-25). This is the most public place a
        # person's name appears — it is what a peer's library selector shows —
        # and a display name that does not reach it is a display name for
        # nobody.
        owner = admin.name if admin else "Unknown"

    # A node with no configured name is "<Owner>'s Library" rather than a
    # generic product name. The old fallback was a fixed string, which made
    # every library in a peer's selector read identically — the one thing a
    # library selector exists to prevent. Capitalised for the derived form
    # only, so a bare unix username ("flux") reads as a name rather than a
    # login.
    node = current_app.config.get("SHARE_NODE_NAME")
    if not node:
        node = f"{owner[:1].upper()}{owner[1:]}'s Library"
    return node, owner


# ── POST /api/share/enroll ────────────────────────────────────────────────────
# The one unauthenticated-by-token route. The invite code IS the credential:
# unguessable, single-use, expiring.
#
# Rate limited since 2026-08-25 (the July TODO, closed before exposure). The
# code being unguessable assumes a bounded number of guesses; nothing bounded
# them. Separately, every attempt costs a database lookup, so an unlimited
# endpoint is a way to make the machine unhappy without ever guessing anything.

@bp.route("/enroll", methods=["POST"], strict_slashes=False)
@rate_limited("ENROLL_RATE_LIMIT", "ENROLL_RATE_WINDOW", bucket="enroll")
def enroll():
    data = request.get_json(silent=True) or {}
    code = (data.get("invite_code") or "").strip()
    device_label = (data.get("device_label") or "").strip() or None
    if not code:
        return jsonify({"error": "Missing invite code"}), 400

    invite = db.session.query(PeerInvite).filter_by(code_hash=hash_secret(code)).first()
    if invite is None or not invite.is_valid():
        return jsonify({"error": "Invalid or expired invite"}), 401

    peer = invite.peer
    if peer is None or not peer.is_active:
        return jsonify({"error": "This invite is no longer active"}), 403

    # Invites are reusable (2026-08-30) -- only stamp the FIRST-use timestamp,
    # never re-consume. This still drives the "Used · joined <date>" admin
    # display; it no longer blocks a second device from enrolling with the
    # same code.
    if invite.consumed_at is None:
        invite.consumed_at = _utcnow()
    raw_token = generate_token()
    token = PeerToken(
        peer_id=peer.id,
        token_hash=hash_secret(raw_token),
        device_label=device_label,
    )
    db.session.add(token)
    db.session.commit()

    node_name, owner_name = _node_identity()
    return jsonify({
        "token":       raw_token,          # shown to the client ONCE — it stores this
        "node_name":   node_name,
        "owner_name":  owner_name,
        "peer_name":   peer.name,
    }), 201


# ── GET /api/share/me ─────────────────────────────────────────────────────────

@bp.route("/me", strict_slashes=False)
@peer_required
def me():
    peer = current_peer()
    node_name, owner_name = _node_identity()
    return jsonify({
        "peer_name":         peer.name,
        "node_name":         node_name,
        "owner_name":        owner_name,
        "collection_count":  len(peer_granted_collection_ids(peer)),
        # Whether /api/share/me/image will answer. A boolean, not a URL: the
        # consumer builds its own proxied URL and never addresses this host.
        "owner_has_image":   bool(_owner_user() and _owner_user().avatar_ext),
    })


# ── GET /api/share/me/image ───────────────────────────────────────────────────
# The owner's picture. Deliberately under the `me` prefix rather than a new
# top-level one: `me` is already relayed by the proxy and already exempt from
# client-side rewriting, so this adds a route without touching THE THREE LISTS
# (share.py / _ALLOWED_PREFIXES / REMOTE_CAPABLE — see test_peer_surface_parity).
#
# It is catalog metadata about a PERSON, not about their holdings, so it reveals
# nothing about what they own — the same reasoning that lets a peer see a full
# performer page.

@bp.route("/me/image", strict_slashes=False)
@peer_required
def owner_image():
    from app.api.auth import _avatar_path
    from app.utils.entity_images import ALLOWED_IMAGE_EXTS

    owner = _owner_user()
    path = _avatar_path(owner) if owner else None
    if not path or not path.exists():
        return jsonify({"error": "No picture"}), 404
    return send_file(str(path),
                     mimetype=ALLOWED_IMAGE_EXTS.get(owner.avatar_ext, "image/jpeg"))


# ── GET /api/share/collections ────────────────────────────────────────────────

@bp.route("/collections", strict_slashes=False)
@peer_required
def list_collections():
    peer = current_peer()
    collections = _peer_visible_collections(peer)

    # System collections are hidden from the peer's collection list (2026-08-24).
    # A Full Library grant means the peer is already browsing the whole library;
    # listing a collection whose contents are exactly that library is noise
    # pretending to be navigation — the same argument that dropped the dimension
    # indexes for 3-show grants, applied the other way round.
    collections = [c for c in collections if not c.is_system]

    collections.sort(key=lambda c: (c.name or "").lower())
    return jsonify([
        {
            "id":              c.id,
            "name":            c.name,
            "description":     c.description,
            # Model-resolved: len(c.recordings) both materialises every row and
            # reports 0 for a dynamic collection.
            "recording_count": c.recording_count,
        }
        for c in collections
    ])


# ── GET /api/share/collections/<id> ───────────────────────────────────────────

@bp.route("/collections/<int:collection_id>", strict_slashes=False)
@peer_required
def collection_detail(collection_id):
    peer = current_peer()
    collection = db.session.get(Collection, collection_id)
    if collection is None:
        abort(404)
    # Visible-set derived, not grant-derived (2026-08-24): a collection is
    # reachable if the peer can see something in it. Under the MVP's
    # share-everything policy that is every curated collection; under selective
    # sharing it narrows on its own, with no second rule to keep in step.
    visible = peer_visible_recording_ids(peer)
    if collection.is_system or not (collection.resolved_recording_ids() & visible):
        abort(403)
    # card=True (2026-08-08): adds genre, genre_color and image_id, which the
    # handbill Browse cards need. Without it a peer's collection renders as
    # colourless cards with initials where every photo should be.
    return jsonify({
        "id":          collection.id,
        "name":        collection.name,
        "description": collection.description,
        # Contents filtered to the visible set — the collection may legitimately
        # hold more than this peer may see once selective sharing returns.
        "recordings":  [_peer_row(r, card=True)
                        for r in collection.recordings if r.id in visible],
    })


# ── GET /api/share/recordings/<id> ────────────────────────────────────────────
# Full metadata (decision #4: peers see everything) — MINUS the internal
# recording-event log, which is this instance's own edit history, not shared
# metadata. Track stream_urls point at the PEER stream endpoint (transcoded),
# never the local FLAC endpoint (which a peer token can't reach anyway).

@bp.route("/recordings/<int:recording_id>", strict_slashes=False)
@peer_required
def recording_detail(recording_id):
    peer = current_peer()
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        abort(404)
    if not peer_can_access_recording(peer, rec):
        abort(403)

    p = rec.performance
    v = p.venue if p else None

    def _analysis(ta):
        """
        Serialise a TrackAnalysis row, or None if the full analysis has not run.

        Mirrors api/recordings.py::_analysis — a "signals" row is the partial
        written during ingest (non-music measurements only), and handing a peer
        a dict of nulls with an empty waveform says "analysed, and everything
        about it is unknown" rather than "not analysed yet". Both serialisers
        read the same table and must answer the same way.
        """
        from app.utils.analysis import SIGNALS_ONLY_VERSION
        if ta is None or ta.analysis_version == SIGNALS_ONLY_VERSION:
            return None
        return {
            "sample_rate_hz":       ta.sample_rate_hz,
            "bit_depth":            ta.bit_depth,
            "bitrate_kbps":         ta.bitrate_kbps,
            "rms_db":               ta.rms_db,
            "peak_db":              ta.peak_db,
            "noise_floor_db":       ta.noise_floor_db,
            "dynamic_range_db":     ta.dynamic_range_db,
            "spectral_cutoff_hz":   ta.spectral_cutoff_hz,
            "waveform":             _json.loads(ta.waveform_json) if ta.waveform_json else [],
        }

    return jsonify({
        "id":               rec.id,
        # The recording page's NEXT call: it fetches /performances/<id> for
        # performer, date, venue and personnel. Omitting this (2026-08-08) left
        # the page with nothing to look up, so it rendered its local empty
        # state — "Set performer / Add date / Add venue" — as though the record
        # were unfilled rather than the fetch never having happened.
        "performance_id":   rec.performance_id,
        "title":            rec.title,
        # The VIEWER's star, not the owner's (2026-08-24). A peer has no
        # favourites of their own yet, so this is False — see _peer_row.
        "is_favorite":      False,
        # Show identity (self-contained, so the peer client needs no other call)
        "performer":        p.performer.name if (p and p.performer) else None,
        # Nav ids (2026-08-08). Milestone 1 sent names only, which was right
        # when a peer had nowhere to navigate TO. With entity pages, the
        # frontend builds #/performer/<id> and #/venue/<id> from exactly these
        # two fields — without them the pages exist but are unreachable.
        # Not a leak: both endpoints are access-checked, so an id for something
        # ungranted buys a 403 and nothing else.
        "performer_id":     p.performer_id if p else None,
        "date":             format_partial_date(p.start_year, p.start_month, p.start_day) if p else None,
        "venue":            v.name    if v else None,
        "venue_id":         v.id      if v else None,
        "city":             v.city    if v else (p.city    if p else None),
        "state":            v.state   if v else (p.state   if p else None),
        "country":          v.country if v else (p.country if p else None),
        # Archivist metadata (the whole payload, read-only)
        "source":           rec.source,
        "lineage":          rec.lineage,
        "quality":          rec.quality,
        "is_complete":      rec.is_complete,
        "is_official":      bool(rec.is_official),
        "info_file_content": rec.info_file_content,
        "notes":            rec.notes,
        "tracks": [
            {
                "id":           t.id,
                "track_number": t.track_number,
                "title":        t.title,
                "set_number":   t.set_number,
                "duration":     t.duration,
                "is_official":  bool(t.is_official),
                "flags":        _json.loads(t.flags) if t.flags else [],
                "songwriter":   t.songwriter,
                "notes":        t.notes,
                "stream_url":   f"/api/share/stream/{t.id}",
                "analysis":     _analysis(t.analysis),
                "checksum": {
                    "type":        t.checksum_type,
                    "status":      t.checksum_status,
                } if t.checksum_type else None,
            }
            for t in rec.tracks
        ],
        "fingerprints": [
            {"type": fp.fingerprint_type, "filename": fp.filename}
            for fp in rec.fingerprints
        ],
    })


# ── GET /api/share/stream/<track_id> ──────────────────────────────────────────
# The one enforcement point that matters: same access check as browse, then a
# transcoded (never raw-FLAC) stream. Logs one access row per play-start.

@bp.route("/stream/<int:track_id>", strict_slashes=False)
@peer_required
def stream(track_id):
    peer = current_peer()
    track = db.session.get(Track, track_id)
    if track is None:
        abort(404)
    if not peer_can_access_track(peer, track):
        abort(403)

    try:
        path = tx.get_or_create_transcode(track)
    except tx.SourceMissing:
        abort(404)
    except tx.FfmpegMissing:
        # Transcoder unavailable — the server box is missing ffmpeg.
        return jsonify({"error": "Transcoder unavailable"}), 503
    except RuntimeError as e:
        current_app.logger.warning("transcode failed for track %s: %s", track_id, e)
        return jsonify({"error": "Transcode failed"}), 500

    # Log one row per play-start: no Range header, or a Range that starts at 0.
    # Seeks (Range starting mid-file) don't re-log, so this counts plays, not
    # every chunk the player requests.
    range_header = request.headers.get("Range", "")
    is_play_start = (not range_header) or "=0-" in range_header.replace(" ", "")
    if is_play_start:
        db.session.add(PeerAccessLog(peer_id=peer.id, track_id=track.id))
        db.session.commit()

    return _serve_file(path, mimetype=tx.mimetype_for())


# ══════════════════════════════════════════════════════════════════════════════
# ENTITY PAGES  (milestone 2, 2026-08-08)
# ══════════════════════════════════════════════════════════════════════════════
#
# Ryan's requirement: a peer should get a HOLISTIC experience — learn about the
# performer, see the venue, see who played that night — not a bare list of
# streamable files. So these mirror the LOCAL endpoints' paths and payload
# shapes exactly, which is what lets the consumer's frontend reuse its existing
# render functions instead of growing a parallel set of peer-only pages.
#
# The distinction that makes this safe (Peer UX Design Spec v1 §2):
#
#   CATALOG METADATA — bio, dossier, photo, genre, lineup, venue name/city.
#       Reference data about the WORLD. Reveals nothing about what is held.
#   HOLDINGS — which recordings exist here. The only thing grants control.
#
# So a peer sees the full Allman Brothers page and only the 3 shows they were
# granted, never all 41.
#
# THE RULE: no query below may reach outside peer_visible_recording_ids(). That
# includes every COUNT — the recording lists are the obvious leak and get got
# right; the subtle one is a count computed over the whole library, which
# publishes the size of a collection that was never shared.

_SHARE_IMG_URL = "/api/share/performers/images"


def _peer_row(rec, card=False):
    """recording_row, with the owner's star removed.

    `is_favorite` means "the VIEWER starred this" everywhere in the UI — it
    renders as the star you click. Passing the OWNER's value through would
    scatter Ryan's private bookmarks across Matt's screen while looking, to
    Matt, like his own.

    The owner's favourites deliberately do not travel (2026-08-24): the star is
    only free to mean "this one is special" while it stays private, and
    Collections is the surface built for curation that IS meant to be read.
    False is the honest answer here, not a redaction — a peer genuinely has no
    favourites yet. When peer-side favourites are built, this is where the
    viewer's own value gets filled in.
    """
    row = recording_row(rec, card=card)
    row["is_favorite"] = False
    return row


def _peer_summary(rec):
    """recording_summary, same reasoning as _peer_row."""
    row = recording_summary(rec)
    if "is_favorite" in row:
        row["is_favorite"] = False
    return row


def _peer_visible_collections(peer):
    """Curated collections holding at least one recording this peer can see.

    ONE rule for both worlds (2026-08-24). Under the MVP's share-everything
    policy this returns every curated collection — Matt sees the curator's
    shelves, which is the intended experience. When selective sharing returns,
    the same code narrows correctly, because it asks about the VISIBLE SET
    rather than about the grant list. Do not special-case "share everything"
    anywhere; let it fall out of the visible set being everything.

    System collections are excluded: a collection whose contents are exactly
    the library you are already browsing is noise pretending to be curation.
    """
    visible = peer_visible_recording_ids(peer)
    if not visible:
        return []
    return [c for c in db.session.query(Collection).all()
            if not c.is_system and (c.resolved_recording_ids() & visible)]


def _visible_performances(peer, performances):
    """Filter a performance collection to the peer's world, preserving order."""
    visible = peer_visible_performance_ids(peer)
    return [p for p in performances if p.id in visible]


def _visible_recordings(peer, recordings):
    visible = peer_visible_recording_ids(peer)
    return [r for r in recordings if r.id in visible]


# ── GET /api/share/performers/<id> ────────────────────────────────────────────
# Pure catalog metadata. Note there is NOTHING to filter here: the local
# endpoint carries no holdings at all — holdings live in the separate
# /recordings sub-route below. The only change from the local payload is the
# image URL prefix, because a peer cannot reach /api/performers/images/<id>.

@bp.route("/performers/<int:performer_id>", strict_slashes=False)
@peer_required
def performer_detail(performer_id):
    from app.models.performer import Performer
    from app.utils import entity_images as ei
    from app.api.performers import _serialize_roster

    peer = current_peer()
    if not peer_can_access_performer(peer, performer_id):
        abort(403)
    p = db.session.get(Performer, performer_id)
    if p is None:
        abort(404)

    return jsonify({
        "id":        p.id,
        "name":      p.name,
        "sort_name": p.sort_name,
        "bio":       p.bio,
        "default_personnel_mode": p.default_personnel_mode,
        "members":   _serialize_roster(p),
        "resources": [{"id": r.id, "label": r.label, "url": r.url} for r in p.resources],
        "has_image": bool(p.images),
        "images":    [ei.image_payload(i, _SHARE_IMG_URL) for i in p.images],
        "dossier":   _json.loads(p.dossier_json) if p.dossier_json else None,
        "genre":     {"id": p.genre.id, "name": p.genre.name,
                      "color": p.genre.color} if p.genre else None,
        "musicbrainz": {
            "mbid":           p.mbid,
            "status":         p.mb_status,
            "type":           p.mb_type,
            "area":           p.mb_area,
            "begin":          p.mb_begin,
            "end":            p.mb_end,
            "disambiguation": p.mb_disambiguation,
            "links":          _json.loads(p.mb_links_json) if p.mb_links_json else {},
            **(_json.loads(p.mb_extra_json) if p.mb_extra_json else {"related": []}),
            "checked_at":     p.mb_checked_at.isoformat() if p.mb_checked_at else None,
        },
    })


# ── GET /api/share/performers/<id>/recordings ─────────────────────────────────
# Holdings. Filtered twice over: performances the peer can't see are dropped
# entirely, and a visible performance's recordings are themselves filtered —
# two tapers of one night can land in different collections.

@bp.route("/performers/<int:performer_id>/recordings", strict_slashes=False)
@peer_required
def performer_recordings(performer_id):
    from app.models.performance import Performance

    peer = current_peer()
    if not peer_can_access_performer(peer, performer_id):
        abort(403)

    visible_perf_ids = peer_visible_performance_ids(peer)
    performances = (
        db.session.query(Performance)
        .filter(Performance.performer_id == performer_id,
                Performance.id.in_(visible_perf_ids))
        .order_by(
            Performance.start_year.desc().nullsfirst(),
            Performance.start_month.desc().nullsfirst(),
            Performance.start_day.desc().nullsfirst(),
        ).all()
    )

    out = []
    for perf in performances:
        v = perf.venue
        recs = _visible_recordings(peer, perf.recordings)
        if not recs:
            continue
        out.append({
            "performance_id": perf.id,
            "performer_name": perf.performer.name if perf.performer else None,
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
            "recordings":     [_peer_summary(r) for r in recs],
        })
    return jsonify(out)


# ── GET /api/share/performers/images/<image_id> ───────────────────────────────
# The photo route. Access is checked against the image's OWNING performer, not
# the image id — otherwise a peer could walk image ids and pull the face of
# every act in a library they were never granted.

@bp.route("/performers/images/<int:image_id>", strict_slashes=False)
@peer_required
def performer_image(image_id):
    from app.models.performer_image import PerformerImage
    from app.utils import entity_images as ei
    from app.api.performers import _performer_images_dir

    peer = current_peer()
    img = db.session.get(PerformerImage, image_id)
    if not img:
        abort(404)
    if not peer_can_access_performer(peer, img.performer_id):
        abort(403)
    return ei.handle_serve(img, _performer_images_dir(img.performer))


# ── GET /api/share/venues/<id> ────────────────────────────────────────────────
# The count-leak endpoint. Local `get_venue` returns performance_count and
# recording_count over the venue's ENTIRE history; served unfiltered to a peer
# that publishes exactly how much of that venue Ryan holds. Both counts here
# are computed over the filtered lists.

@bp.route("/venues/<int:venue_id>", strict_slashes=False)
@peer_required
def venue_detail(venue_id):
    from app.models.venue import Venue
    from app.utils import entity_images as ei

    peer = current_peer()
    if not peer_can_access_venue(peer, venue_id):
        abort(403)
    v = db.session.get(Venue, venue_id)
    if v is None:
        abort(404)

    perfs = sorted(
        _visible_performances(peer, v.performances),
        key=lambda p: (p.start_year or 0, p.start_month or 0, p.start_day or 0),
    )
    recordings = [_peer_row(r, card=True)
                  for p in perfs for r in _visible_recordings(peer, p.recordings)]

    return jsonify({
        "id":                v.id,
        "name":              v.name,
        "city":              v.city,
        "state":             v.state,
        "country":           v.country,
        "bio":               v.bio,
        # Counts over the VISIBLE set — see the rule at the top of this section.
        "performance_count": len(perfs),
        "recording_count":   len(recordings),
        "recordings":        recordings,
        # Venue photos are not exposed to peers: venue_image has zero rows
        # (2026-08-08), so a peer image route for it would be untested code
        # guarding an empty table. Add it when venues actually have photos.
        "has_image":         False,
        "images":            [],
    })


# ── GET /api/share/artists/<id> ───────────────────────────────────────────────
# The people. `performers` is narrowed to acts the peer can see — an artist
# page listing bands whose shows aren't shared would name acts by the back
# door. Guest appearances are filtered to visible performances.

@bp.route("/artists/<int:artist_id>", strict_slashes=False)
@peer_required
def artist_detail(artist_id):
    from app.models.artist import Artist
    from app.models.performance_personnel import PerformancePersonnel

    peer = current_peer()
    if not peer_can_access_artist(peer, artist_id):
        abort(403)
    a = db.session.get(Artist, artist_id)
    if a is None:
        abort(404)

    visible_performer_ids = peer_visible_performer_ids(peer)
    performers = [m.performer for m in a.memberships
                  if m.performer is not None and m.performer.id in visible_performer_ids]
    performers.sort(key=lambda p: (p.sort_name or p.name).lower())
    member_performer_ids = {p.id for p in performers}

    visible_perf_ids = peer_visible_performance_ids(peer)
    guest_appearances = []
    for pp in db.session.query(PerformancePersonnel).filter_by(artist_id=artist_id).all():
        perf = pp.performance
        if not perf or perf.id not in visible_perf_ids:
            continue
        if perf.performer_id in member_performer_ids:
            continue
        recs = _visible_recordings(peer, perf.recordings)
        if not recs:
            continue
        v = perf.venue
        guest_appearances.append({
            "performance_id": perf.id,
            "performer_id":   perf.performer_id,
            "performer_name": perf.performer.name if perf.performer else None,
            "date":       format_partial_date(perf.start_year, perf.start_month, perf.start_day),
            "start_year": perf.start_year, "start_month": perf.start_month,
            "start_day":  perf.start_day,
            "venue_name": v.name    if v else None,
            "city":       v.city    if v else perf.city,
            "state":      v.state   if v else perf.state,
            "country":    v.country if v else perf.country,
            "instrument": pp.instrument,
            "is_guest":   pp.is_guest,
            "note":       pp.note,
            "recordings": [_peer_summary(r) for r in recs],
        })
    guest_appearances.sort(
        key=lambda g: (g["start_year"] or 0, g["start_month"] or 0, g["start_day"] or 0))

    return jsonify({
        "id":        a.id,
        "name":      a.name,
        "sort_name": a.sort_name,
        "bio":       a.bio,
        "performers":        [{"id": p.id, "name": p.name} for p in performers],
        "guest_appearances": guest_appearances,
    })


# ── GET /api/share/genres/ ────────────────────────────────────────────────────
# Local list_genres computes performer_count and recording_count with a GROUP BY
# over the whole library. Reproduced here in Python over the visible set only,
# which is cheap at this scale and impossible to accidentally leave unfiltered.
# Genres with nothing visible are omitted entirely rather than shown as zero —
# a zero row still names a genre the peer has no access to.

@bp.route("/genres/", strict_slashes=False)
@peer_required
def list_genres():
    from app.models.genre import Genre
    from app.models.performance import Performance
    from app.models.performer import Performer

    peer = current_peer()
    visible_recs = peer_visible_recording_ids(peer)
    if not visible_recs:
        return jsonify([])

    rows = (db.session.query(Performer.genre_id, Performer.id, Recording.id)
            .join(Performance, Performance.performer_id == Performer.id)
            .join(Recording, Recording.performance_id == Performance.id)
            .filter(Recording.id.in_(visible_recs),
                    Performer.genre_id.isnot(None))
            .all())

    performers_by_genre = {}
    recordings_by_genre = {}
    for genre_id, performer_id, recording_id in rows:
        performers_by_genre.setdefault(genre_id, set()).add(performer_id)
        recordings_by_genre.setdefault(genre_id, set()).add(recording_id)

    if not performers_by_genre:
        return jsonify([])

    genres = (db.session.query(Genre)
              .filter(Genre.id.in_(performers_by_genre.keys()))
              .order_by(Genre.name).all())
    return jsonify([
        {
            "id":              g.id,
            "name":            g.name,
            "description":     g.description,
            "color":           g.color,
            "performer_count": len(performers_by_genre.get(g.id, ())),
            "recording_count": len(recordings_by_genre.get(g.id, ())),
        }
        for g in genres
    ])


# ── GET /api/share/genres/<id> ────────────────────────────────────────────────
# Mirrors local get_genre. Performers are narrowed to those with something
# visible, their recording lists to the visible set, and BOTH counts are
# recomputed from the filtered rows rather than carried over from the genre's
# true totals. A genre whose every recording is ungranted 404s rather than
# rendering as an empty page — an empty page still confirms the genre is here.

@bp.route("/genres/<int:genre_id>", strict_slashes=False)
@peer_required
def genre_detail(genre_id):
    from app.models.genre import Genre
    from app.models.performance import Performance
    from app.models.performer import Performer
    from sqlalchemy import func

    peer = current_peer()
    g_row = db.session.get(Genre, genre_id)
    if g_row is None:
        abort(404)

    visible_performer_ids = peer_visible_performer_ids(peer)
    visible_perf_ids = peer_visible_performance_ids(peer)
    if not visible_performer_ids:
        abort(403)

    performers = (
        db.session.query(Performer)
        .filter(Performer.genre_id == genre_id,
                Performer.id.in_(visible_performer_ids))
        .order_by(func.coalesce(Performer.sort_name, Performer.name))
        .all()
    )
    if not performers:
        abort(403)

    perf_rows = []
    total_recordings = 0
    for p in performers:
        performances = (
            db.session.query(Performance)
            .filter(Performance.performer_id == p.id,
                    Performance.id.in_(visible_perf_ids))
            .order_by(
                Performance.start_year.desc().nullsfirst(),
                Performance.start_month.desc().nullsfirst(),
                Performance.start_day.desc().nullsfirst(),
            ).all()
        )
        recordings = []
        for perf in performances:
            v = perf.venue
            for r in _visible_recordings(peer, perf.recordings):
                row = _peer_summary(r)
                row.update({
                    "performer":   p.name,
                    "start_year":  perf.start_year,
                    "start_month": perf.start_month,
                    "start_day":   perf.start_day,
                    "venue":       v.name    if v else None,
                    "city":        v.city    if v else perf.city,
                    "state":       v.state   if v else perf.state,
                    "country":     v.country if v else perf.country,
                })
                recordings.append(row)
        if not recordings:
            continue
        total_recordings += len(recordings)
        perf_rows.append({
            "id":              p.id,
            "name":            p.name,
            "recording_count": len(recordings),
            "recordings":      recordings,
        })

    return jsonify({
        "id":              g_row.id,
        "name":            g_row.name,
        "description":     g_row.description,
        "color":           g_row.color,
        "performer_count": len(perf_rows),
        "recording_count": total_recordings,
        "performers":      perf_rows,
    })


# ── GET /api/share/performances/<id> ──────────────────────────────────────────
# The recording page's second call. app.js fetches the RECORDING for tracks and
# metadata, then the PERFORMANCE for who/when/where — performer, date, venue and
# resolved personnel live here, not on the recording.
#
# Missing it (2026-08-08) is why a peer's View Recording page rendered "Set
# performer / Add date / Add venue": the page had a recording with no
# performance to describe it, so it fell back to its empty-state prompts and
# looked like an unfilled local record rather than a failed remote fetch.

@bp.route("/performances/<int:performance_id>", strict_slashes=False)
@peer_required
def performance_detail(performance_id):
    from app.models.performance import Performance
    from app.utils.personnel import resolve_performance_personnel

    peer = current_peer()
    if performance_id not in peer_visible_performance_ids(peer):
        abort(403)
    p = db.session.get(Performance, performance_id)
    if p is None:
        abort(404)

    v = p.venue
    resolved = resolve_performance_personnel(p)
    return jsonify({
        "id":             p.id,
        "performer_id":   p.performer_id,
        "performer":      p.performer.name if p.performer else None,
        "members":        [{"id": r["artist_id"], "name": r["name"]} for r in resolved],
        "personnel":      resolved,
        "personnel_mode": p.personnel_mode,
        "title":        p.title,
        "stage":        p.stage,
        "start_year":   p.start_year,
        "start_month":  p.start_month,
        "start_day":    p.start_day,
        "end_year":     p.end_year,
        "end_month":    p.end_month,
        "end_day":      p.end_day,
        "venue_id":     v.id      if v else None,
        "venue_name":   v.name    if v else None,
        "city":         v.city    if v else p.city,
        "state":        v.state   if v else p.state,
        "country":      v.country if v else p.country,
        "event_id":     p.event_id,
        "event_name":   p.event.name if p.event else None,
        "notes":        p.notes,
        # Only the recordings of this show that the peer was actually granted —
        # two tapers of one night can sit in different collections.
        "recordings":   [_peer_summary(r) for r in _visible_recordings(peer, p.recordings)],
    })


# ══════════════════════════════════════════════════════════════════════════════
# BROWSE MODULES  (2026-08-08)
# ══════════════════════════════════════════════════════════════════════════════
#
# Ryan's call: a peer gets a real Library page built from the granted set, not
# just a list of collections. app.js composes Browse from five independent
# fetches and hides any module whose fetch fails or comes back empty, so these
# mirror the local paths one for one and the page assembles itself.
#
# The Recommended algorithm is NOT reimplemented here. Its diversity rules
# (distinct performer as a hard rule, distinct genre as a soft preference,
# unplayed weighting, date-stable seeding) are subtle enough that a second copy
# would drift within a month. The helpers are imported and fed a filtered pool
# instead — the same compromise made for _serialize_roster, and for the same
# reason: they are pure functions that happen to live in a views module.

@bp.route("/recordings/recent", strict_slashes=False)
@peer_required
def recent_recordings():
    """Recently Added, scoped. Ingest order is the owner's, not the peer's —
    a peer sees the owner's most recently added SHARED shows, which is the
    only meaning available and a reasonable one."""
    peer = current_peer()
    visible = peer_visible_recording_ids(peer)
    if not visible:
        return jsonify([])

    limit = request.args.get("limit", 50, type=int) or 50
    limit = max(1, min(limit, 200))
    card = request.args.get("card", "").lower() in ("1", "true", "yes")

    from app.api.recordings import _card_eager
    query = Recording.query.filter(Recording.id.in_(visible))
    if card:
        query = _card_eager(query)
    recs = query.order_by(Recording.created_at.desc()).limit(limit).all()
    return jsonify([_peer_row(r, card=card) for r in recs])


@bp.route("/recordings/recommended", strict_slashes=False)
@peer_required
def recommended_recordings():
    import random
    from datetime import date as _date
    from app.api.recordings import (
        _card_eager, _recommended_pool_query, _genre_by_performer, _select_diverse,
    )

    peer = current_peer()
    visible = peer_visible_recording_ids(peer)
    if not visible:
        return jsonify([])

    limit = request.args.get("limit", 3, type=int) or 3
    limit = max(1, min(limit, 12))
    reroll = request.args.get("reroll", 0, type=int) or 0

    # The one change from local: the A/A+ pool is intersected with the visible
    # set BEFORE selection, so diversity is computed over what the peer can
    # actually play rather than over the whole library.
    pool = (_card_eager(_recommended_pool_query())
            .filter(Recording.id.in_(visible)).all())
    if not pool:
        return jsonify([])

    # No play-history weighting: play_log records the OWNER's listening, which
    # is none of the peer's business and no guide to what they have heard.
    perf_by_rec = {r.id: (r.performance.performer_id if r.performance else None)
                   for r in pool}
    genre_by_performer = _genre_by_performer(
        {pid for pid in perf_by_rec.values() if pid is not None})

    rnd = random.Random(f"{_date.today().isoformat()}:{reroll}")
    ordered = list(pool)
    rnd.shuffle(ordered)

    picks = _select_diverse(ordered, limit, perf_by_rec, genre_by_performer)
    return jsonify([_peer_row(r, card=True) for r in picks])


@bp.route("/recordings/on-this-day", strict_slashes=False)
@peer_required
def on_this_day():
    from app.models.performance import Performance

    peer = current_peer()
    visible = peer_visible_recording_ids(peer)
    if not visible:
        return jsonify([])

    today = datetime.now(timezone.utc).date()
    recs = (Recording.query
            .join(Performance, Recording.performance_id == Performance.id)
            .filter(Recording.id.in_(visible),
                    Performance.start_month == today.month,
                    Performance.start_day == today.day)
            .order_by(Performance.start_year.asc().nullslast())
            .all())
    return jsonify([_peer_row(r) for r in recs])


@bp.route("/recordings/by-ids", strict_slashes=False)
@peer_required
def recordings_by_ids():
    """
    Batch lookup: `?ids=3,17,42`. Used by the consumer to render ITS OWN
    favourites, which are stored as bare ids because nothing about the
    recording is cached locally (see models/remote_favorite.py).

    One call rather than N — a sidebar rendering a dozen favourites through the
    proxy one at a time would be a dozen round trips over someone's home
    internet connection.

    Ids outside the visible set are simply ABSENT from the response, never an
    error. The caller is asking about rows it believes it can see, and a 403
    for one id in a batch of twelve would turn a rendering problem into a
    failure. A favourite whose recording is no longer shared should quietly
    stop appearing, which is what the sharer's revocation MEANT.
    """
    peer = current_peer()
    visible = peer_visible_recording_ids(peer)
    if not visible:
        return jsonify([])

    raw = (request.args.get("ids") or "").strip()
    if not raw:
        return jsonify([])

    wanted = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            wanted.add(int(part))
    # Bounded so a hand-crafted query cannot ask for the whole library in one
    # go by listing every integer.
    wanted = set(list(wanted & visible)[:500])
    if not wanted:
        return jsonify([])

    card = request.args.get("card", "").lower() in ("1", "true", "yes")
    from app.api.recordings import _card_eager
    query = Recording.query.filter(Recording.id.in_(wanted))
    if card:
        query = _card_eager(query)
    recs = query.all()
    return jsonify([_peer_row(r, card=card) for r in recs])


@bp.route("/venues/", strict_slashes=False)
@peer_required
def list_venues():
    """Venues with at least one visible recording. Mirrors local list_venues."""
    from app.models.venue import Venue
    from app.models.performance import Performance

    peer = current_peer()
    venue_ids = peer_visible_venue_ids(peer)
    if not venue_ids:
        return jsonify([])

    visible_perfs = peer_visible_performance_ids(peer)
    venues = (db.session.query(Venue)
              .filter(Venue.id.in_(venue_ids)).order_by(Venue.name).all())

    # Counted over VISIBLE performances only. `len(v.performances)` would
    # publish how many shows the owner really holds at that venue.
    counts = {}
    for (vid,) in (db.session.query(Performance.venue_id)
                   .filter(Performance.id.in_(visible_perfs),
                           Performance.venue_id.isnot(None))):
        counts[vid] = counts.get(vid, 0) + 1

    return jsonify([
        {
            "id":                v.id,
            "name":              v.name,
            "city":              v.city,
            "state":             v.state,
            "country":           v.country,
            "performance_count": counts.get(v.id, 0),
        }
        for v in venues
    ])


@bp.route("/artists/", strict_slashes=False)
@peer_required
def list_artists():
    """People reachable through a visible act. Mirrors local list_artists.

    Membership-derived, like every other artist surface here: a band's lineup
    is catalog metadata about the act, and narrowing it to whoever played the
    visible nights gives incoherent pages.
    """
    from sqlalchemy import func as _func
    from app.models.artist import Artist

    peer = current_peer()
    artist_ids = peer_visible_artist_ids(peer)
    if not artist_ids:
        return jsonify([])

    rows = (db.session.query(Artist)
            .filter(Artist.id.in_(artist_ids))
            .order_by(_func.coalesce(Artist.sort_name, Artist.name)).all())
    return jsonify([
        {"id": a.id, "name": a.name, "sort_name": a.sort_name} for a in rows
    ])


@bp.route("/search", strict_slashes=False)
@peer_required
def search():
    """
    Search, scoped to the visible set.

    api/search.py deliberately shipped WITHOUT a peer route, on the grounds
    that a half-filtered search is worse than none — the peer blueprint's whole
    premise is being structurally incapable of exposing what it should not. So
    this does not re-implement the engine. It borrows the one seam that file
    was built around, `build_search_index()`, and filters the index BEFORE the
    search runs.

    Filtering the INDEX rather than the RESULTS is what makes it safe: every
    group, every count and every "and 31 more" total is computed from rows the
    peer can already see, because nothing else was ever in the index.
    """
    from app.api import search as local_search
    from app.utils import search as se

    q = request.args.get("q", "").strip()
    group_type = request.args.get("type")
    if group_type is not None and group_type not in se.GROUP_ORDER:
        return jsonify({"error": f"unknown type: {group_type}"}), 400

    peer = current_peer()
    visible_recs = peer_visible_recording_ids(peer)
    if not visible_recs:
        return jsonify({"query": q, "text_terms": [], "date_terms": [],
                        "total": 0, "groups": []})

    visible_performers = peer_visible_performer_ids(peer)
    visible_artists = peer_visible_artist_ids(peer)
    visible_venues = peer_visible_venue_ids(peer)

    raw = local_search.build_search_index()
    recordings = [r for r in raw["recordings"] if r["id"] in visible_recs]
    performers = [p for p in raw["performers"] if p["id"] in visible_performers]
    venues = [v for v in raw["venues"] if v["id"] in visible_venues]
    artists = [
        # The act list on an artist row is narrowed too, or a person visible
        # through one band advertises every other band they were ever in.
        {**a, "performer_ids": [pid for pid in a.get("performer_ids", [])
                                if pid in visible_performers]}
        for a in raw["artists"] if a["id"] in visible_artists
    ]
    index = se.build_index(performers, artists, venues, recordings)

    result = se.run_search(index, q)
    counts = local_search._derived_counts(index)

    if group_type:
        limit = local_search._int_arg("limit", 25, 1, local_search.MAX_LIMIT)
        offset = local_search._int_arg("offset", 0, 0, 10_000)
        g = result["groups"][group_type]
        window = g["items"][offset:offset + limit]
        return jsonify({
            "query": result["query"], "text_terms": result["text_terms"],
            "date_terms": result["date_terms"], "type": group_type,
            "label": g["label"], "total": g["total"],
            "offset": offset, "limit": limit,
            "items": local_search._serialise(group_type, window, index, counts),
        })

    limit = local_search._int_arg("limit", local_search.DEFAULT_DROPDOWN_LIMIT,
                                  1, local_search.MAX_LIMIT)
    groups = []
    for key in se.GROUP_ORDER:
        g = result["groups"][key]
        if not g["total"]:
            continue
        groups.append({
            "type": key, "label": g["label"], "total": g["total"],
            "items": local_search._serialise(key, g["items"][:limit], index, counts),
        })

    return jsonify({
        "query": result["query"], "text_terms": result["text_terms"],
        "date_terms": result["date_terms"],
        "total": sum(g["total"] for g in groups),
        "groups": groups,
    })


@bp.route("/performers/all-recordings", strict_slashes=False)
@peer_required
def all_recordings():
    """
    The Library/Browse payload: every visible performer with their visible
    performances and recordings, in one request.

    MIRRORS `GET /api/performers/all-recordings` (api/performers.py) key for
    key, deliberately. The frontend's `renderLibraryView()` consumes this shape
    and `contextualise()` rewrites the URL transparently, so any divergence
    here shows up as a broken Library page on the peer side only — which is
    exactly how this endpoint came to be missing in the first place. The local
    route was added during the Browse rebuild (2026-08-23) and the share door
    never got its counterpart, so a peer's Library said "Failed to load
    library" while everything else worked.

    Every list AND every count is computed over the visible set. A peer must
    not learn that an act they can see three shows by actually has forty-one:
    `performance_count` and `recording_count` below are lengths of the FILTERED
    lists, never of the performer's real holdings.
    """
    from sqlalchemy import func as _func
    from app.models.performance import Performance
    from app.models.performer import Performer

    peer = current_peer()
    visible_performers = peer_visible_performer_ids(peer)
    if not visible_performers:
        return jsonify([])

    visible_perfs = peer_visible_performance_ids(peer)
    visible_recs = peer_visible_recording_ids(peer)

    performers = (
        db.session.query(Performer)
        .filter(Performer.id.in_(visible_performers))
        # coalesce(sort_name, name): sort_name is NULL for every performer in
        # this database, and ordering on it alone ties every row.
        .order_by(_func.coalesce(Performer.sort_name, Performer.name))
        .all()
    )

    result = []
    for pf in performers:
        performances = (
            db.session.query(Performance)
            .filter(Performance.performer_id == pf.id,
                    Performance.id.in_(visible_perfs))
            .order_by(
                Performance.start_year.asc().nullslast(),
                Performance.start_month.asc().nullslast(),
                Performance.start_day.asc().nullslast(),
            ).all()
        )
        if not performances:
            continue

        perf_list = []
        for p in performances:
            recs = [r for r in p.recordings if r.id in visible_recs]
            if not recs:
                # A performance whose every recording is filtered out must not
                # appear as an empty row — that publishes the existence of a
                # show the peer was not granted.
                continue
            v = p.venue
            perf_list.append({
                "performance_id": p.id,
                "performer_name": p.performer.name,
                "title":          p.title,
                "start_year":     p.start_year,
                "start_month":    p.start_month,
                "start_day":      p.start_day,
                "venue_name":     v.name    if v else None,
                "city":           v.city    if v else p.city,
                "state":          v.state   if v else p.state,
                "country":        v.country if v else p.country,
                "recordings":     [_peer_summary(r) for r in recs],
            })
        if not perf_list:
            continue

        # Genre is catalog metadata about the ACT — safe to send in full, and
        # needed for Browse's genre filter and the colour spine on every row.
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


@bp.route("/performers/", strict_slashes=False)
@peer_required
def list_performers():
    """
    Acts with something visible, and counts over the visible set only.

    Note this is a list endpoint, which the entity-page design deliberately
    avoided — a peer must not be able to enumerate a library. It is safe here
    for the same reason the artist page is: it can only ever contain acts the
    peer already reached through a granted recording. The dangerous version
    would be an unfiltered index, and this is not that.
    """
    from app.models.performance import Performance
    from app.models.performer import Performer
    from sqlalchemy import func

    peer = current_peer()
    visible = peer_visible_recording_ids(peer)
    if not visible:
        return jsonify([])

    rows = (db.session.query(Performer, func.count(Recording.id).label("rc"))
            .join(Performance, Performance.performer_id == Performer.id)
            .join(Recording, Recording.performance_id == Performance.id)
            .filter(Recording.id.in_(visible))
            .group_by(Performer.id)
            .order_by(func.coalesce(Performer.sort_name, Performer.name))
            .all())
    return jsonify([
        {
            "id":              p.id,
            "name":            p.name,
            "sort_name":       p.sort_name,
            "recording_count": rc,
            "members":         [a.name for a in p.artists],
            "genre_id":        p.genre_id,
            "genre_name":      p.genre.name if p.genre else None,
        }
        for p, rc in rows
    ])
