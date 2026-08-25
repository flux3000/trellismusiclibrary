"""
api/peers.py — local ADMIN management of the inbound sharing side.

These endpoints are cookie/session-authenticated (@admin_required) — they are
how the owner runs their sharing: create a named peer, grant/revoke
collections, mint the one-time invite string, review who's been streaming.
Entirely distinct from api/share.py, which is the peer-facing (token) door.

Peer management is more sensitive than ordinary metadata editing (it hands out
access to the outside world), so unlike the rest of the app's login_required
CRUD, these carry an explicit admin-role gate. See "Peer Sharing — Design
Spec v1".

Routes (url_prefix /api/peers):
  GET    /                       list peers (brief)
  POST   /                       create a peer
  GET    /<id>                   peer detail (grants, devices, invites)
  PATCH  /<id>                   rename / edit contact note
  POST   /<id>/revoke            revoke the peer entirely (kills all access)
  POST   /<id>/grants            grant collection(s)  {collection_ids:[...]}
  DELETE /<id>/grants/<cid>      revoke one collection grant
  POST   /<id>/invites           mint a fresh invite → returns address#code ONCE
  GET    /<id>/activity          recent stream activity
"""

from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user

from app.extensions import db
from app.models.peer import Peer, CollectionGrant, PeerInvite, PeerToken, PeerAccessLog
from app.models.collection import Collection
from app.models.recording import Recording
from app.models.track import Track
from app.utils.peer_auth import generate_invite_code, hash_secret
from app.utils.format import format_partial_date

bp = Blueprint("peers", __name__)

DEFAULT_INVITE_DAYS = 7


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat() if dt else None


def admin_required(f):
    """login_required + role == admin. Peer management is the sharing control
    surface — hold it to a higher bar than ordinary editing."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if getattr(current_user, "role", None) != "admin":
            return jsonify({"error": "Admin only"}), 403
        return f(*args, **kwargs)
    return wrapper


# ── Serialization ─────────────────────────────────────────────────────────────

def _peer_brief(peer):
    return {
        "id":            peer.id,
        "name":          peer.name,
        "contact_note":  peer.contact_note,
        "is_active":     peer.is_active,
        "created_at":    _iso(peer.created_at),
        "last_seen_at":  _iso(peer.last_seen_at),
        "grant_count":   len([g for g in peer.grants if g.is_active]),
        "device_count":  len([t for t in peer.tokens if t.is_active]),
        # any token ever minted means they've completed enrollment at least once
        "has_joined":    len(peer.tokens) > 0,
        "pending_invites": len([i for i in peer.invites if i.is_valid()]),
    }


def _peer_detail(peer):
    d = _peer_brief(peer)
    d["grants"] = [
        {"collection_id": g.collection_id,
         "collection_name": g.collection.name if g.collection else None,
         "granted_at": _iso(g.created_at)}
        for g in peer.grants if g.is_active
    ]
    d["devices"] = [
        {"id": t.id, "label": t.device_label,
         "created_at": _iso(t.created_at), "last_used_at": _iso(t.last_used_at)}
        for t in peer.tokens if t.is_active
    ]
    # `status` is derived here rather than left to the client to work out from
    # two booleans. Three states that mean genuinely different things:
    #   pending  — a LIVE key to this library. The only one worth cancelling.
    #   used     — someone enrolled with it. History; the device it produced is
    #              the real record, and revoking that is a different action.
    #   expired  — dead of old age. Harmless, but clutter.
    d["invites"] = [
        {"id": i.id,
         "created_at": _iso(i.created_at), "expires_at": _iso(i.expires_at),
         "consumed": i.consumed_at is not None, "valid": i.is_valid(),
         "consumed_at": _iso(i.consumed_at),
         "status": ("used" if i.consumed_at is not None
                    else "pending" if i.is_valid() else "expired")}
        for i in sorted(peer.invites, key=lambda x: x.created_at or _utcnow(),
                        reverse=True)
    ]
    return d


# ── Peer CRUD ─────────────────────────────────────────────────────────────────

@bp.route("/")
@admin_required
def list_peers():
    peers = db.session.query(Peer).order_by(Peer.name).all()
    return jsonify([_peer_brief(p) for p in peers])


@bp.route("/", methods=["POST"])
@admin_required
def create_peer():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    peer = Peer(name=name, contact_note=(data.get("contact_note") or "").strip() or None)
    db.session.add(peer)
    db.session.commit()
    return jsonify(_peer_detail(peer)), 201


@bp.route("/<int:peer_id>")
@admin_required
def get_peer(peer_id):
    peer = db.session.get(Peer, peer_id)
    if not peer:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_peer_detail(peer))


@bp.route("/<int:peer_id>", methods=["PATCH"])
@admin_required
def update_peer(peer_id):
    peer = db.session.get(Peer, peer_id)
    if not peer:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name cannot be empty"}), 400
        peer.name = name
    if "contact_note" in data:
        peer.contact_note = (data.get("contact_note") or "").strip() or None
    db.session.commit()
    return jsonify(_peer_detail(peer))


@bp.route("/<int:peer_id>/revoke", methods=["POST"])
@admin_required
def revoke_peer(peer_id):
    """Kill this peer entirely. Setting peer.revoked_at cascades through the
    is_active properties — every grant and every token instantly reads as
    inactive, so all their access dies at once."""
    peer = db.session.get(Peer, peer_id)
    if not peer:
        return jsonify({"error": "Not found"}), 404
    if peer.revoked_at is None:
        peer.revoked_at = _utcnow()
        db.session.commit()
    return jsonify(_peer_detail(peer))


# ── Grants ────────────────────────────────────────────────────────────────────

@bp.route("/<int:peer_id>/grants", methods=["POST"])
@admin_required
def add_grants(peer_id):
    peer = db.session.get(Peer, peer_id)
    if not peer:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    ids = data.get("collection_ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "collection_ids (non-empty list) required"}), 400

    already_active = {g.collection_id for g in peer.grants if g.is_active}
    added, skipped = [], []
    for cid in ids:
        if cid in already_active:
            skipped.append(cid)
            continue
        if db.session.get(Collection, cid) is None:
            skipped.append(cid)
            continue
        db.session.add(CollectionGrant(peer_id=peer.id, collection_id=cid))
        added.append(cid)
    db.session.commit()
    return jsonify({"added": added, "skipped": skipped, "peer": _peer_detail(peer)})


@bp.route("/<int:peer_id>/grants/<int:collection_id>", methods=["DELETE"])
@admin_required
def revoke_grant(peer_id, collection_id):
    """Soft-revoke the peer's active grant to one collection."""
    grant = db.session.query(CollectionGrant).filter_by(
        peer_id=peer_id, collection_id=collection_id, revoked_at=None).first()
    if grant:
        grant.revoked_at = _utcnow()
        db.session.commit()
    return jsonify({"ok": True})


# ── Invites ───────────────────────────────────────────────────────────────────

@bp.route("/<int:peer_id>/invites", methods=["POST"])
@admin_required
def mint_invite(peer_id):
    """Generate a fresh one-time invite. The raw code (and the compound
    address#code string the peer pastes) is returned HERE and nowhere else —
    only its hash is stored. `invite` is null until SHARE_BASE_URL is set
    (the public address, configured in the infra milestone)."""
    peer = db.session.get(Peer, peer_id)
    if not peer:
        return jsonify({"error": "Not found"}), 404
    if not peer.is_active:
        return jsonify({"error": "Peer is revoked"}), 400

    data = request.get_json(silent=True) or {}
    try:
        days = int(data.get("expires_days") or DEFAULT_INVITE_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_INVITE_DAYS
    days = max(1, min(days, 90))

    raw_code = generate_invite_code()
    expires_at = _utcnow() + timedelta(days=days)
    db.session.add(PeerInvite(
        peer_id=peer.id, code_hash=hash_secret(raw_code), expires_at=expires_at))
    db.session.commit()

    base_url = current_app.config.get("SHARE_BASE_URL")
    invite_string = f"{base_url.rstrip('/')}#{raw_code}" if base_url else None
    return jsonify({
        "code":         raw_code,          # shown ONCE
        "invite":       invite_string,     # the single string to send the peer (or null)
        "base_url_set": bool(base_url),
        "expires_at":   _iso(expires_at),
    }), 201


@bp.route("/<int:peer_id>/invites/<int:invite_id>", methods=["DELETE"])
@admin_required
def delete_invite(peer_id, invite_id):
    """
    Cancel an unused invite, or clear an expired one.

    This is NOT "Revoke Access" and the difference matters (Ryan, 2026-08-25 —
    revoking was the only thing on offer, and it means something else
    entirely). Revoking kills the PEER: every device they hold stops working
    and their library goes dark. Cancelling an invite only makes one unused
    code stop working. Nobody who already joined is affected.

    A USED invite is never deletable. It is the record that this peer enrolled,
    and the token it minted is the thing you would actually want to kill — that
    is a device revocation, a different control again. Deleting the row here
    would quietly erase the audit trail for an access that still exists.

    An unused invite genuinely has no history worth keeping — nobody ever
    presented it — so this is a real delete rather than a soft one, and no
    schema change was needed to add it.
    """
    invite = db.session.get(PeerInvite, invite_id)
    if invite is None or invite.peer_id != peer_id:
        return jsonify({"error": "Not found"}), 404

    if invite.consumed_at is not None:
        return jsonify({
            "error": "That invite was used to join. Remove the device instead — "
                     "deleting this would erase the record of an access that "
                     "still works.",
            "code":  "invite_consumed",
        }), 409

    was_live = invite.is_valid()
    db.session.delete(invite)
    db.session.commit()
    # Say which of the two things just happened, so the UI can word it honestly.
    return jsonify({"deleted": invite_id,
                    "was": "pending" if was_live else "expired"}), 200


# ── Activity ──────────────────────────────────────────────────────────────────

@bp.route("/<int:peer_id>/activity")
@admin_required
def peer_activity(peer_id):
    peer = db.session.get(Peer, peer_id)
    if not peer:
        return jsonify({"error": "Not found"}), 404
    limit = request.args.get("limit", 50, type=int) or 50
    limit = max(1, min(limit, 200))
    rows = (db.session.query(PeerAccessLog)
            .filter_by(peer_id=peer_id)
            .order_by(PeerAccessLog.occurred_at.desc())
            .limit(limit).all())

    out = []
    for r in rows:
        track = db.session.get(Track, r.track_id)
        rec = db.session.get(Recording, track.recording_id) if track else None
        p = rec.performance if rec else None
        out.append({
            "occurred_at":   _iso(r.occurred_at),
            "track_id":      r.track_id,
            "track_title":   track.title if track else None,
            "recording_id":  rec.id if rec else None,
            "performer":     p.performer.name if (p and p.performer) else None,
            "date":          format_partial_date(p.start_year, p.start_month, p.start_day) if p else None,
        })
    return jsonify(out)
