"""
api/remote_favorites.py — MY favourites inside libraries I have joined.

Routes (url_prefix /api/remote-favorites):
  GET    /<node_id>            rows for the sidebar (resolved from the remote)
  GET    /<node_id>/ids        just the ids — cheap, local, for painting stars
  POST   /<node_id>            { recording_id }  star it
  DELETE /<node_id>/<rec_id>   unstar it

⚠ WHY A SEPARATE PREFIX, NOT /api/remotes/<id>/favorites
--------------------------------------------------------
`/api/remotes/<id>/<path:subpath>` is the generic PROXY, and it forwards to the
remote. A favourites path living under it would be ambiguous at best — a GET
would be relayed to the sharer's door (which has no such route, by design) and
a POST would depend on Werkzeug preferring a static segment over a `path:`
converter. That is a routing subtlety to bet a feature on.

The prefix also says the true thing: these rows are LOCAL. Nothing here is
proxied except the final resolution of ids into displayable recordings, and
that is an implementation detail of GET, not of the resource.

⚠ WHY THE STAR IS NOT AN EDIT
-----------------------------
`api.js` refuses every non-GET whose path would be proxied, because writing to
a library that is not mine must fail loudly rather than 403 confusingly. That
backstop is correct and stays. Starring is not an exception to it — starring
never touches the remote at all. It writes one row here, about their recording,
and the sharer never learns of it.
"""

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app.extensions import db
from app.models.remote_favorite import RemoteFavorite
from app.models.remote_node import RemoteNode
from app.api.remotes import fetch_remote_json

bp = Blueprint("remote_favorites", __name__)


def _node_or_404(node_id):
    node = db.session.get(RemoteNode, node_id)
    if not node or not node.is_active:
        return None, (jsonify({"error": "Not found"}), 404)
    return node, None


def _favorite_ids(node_id):
    rows = (db.session.query(RemoteFavorite.remote_recording_id)
            .filter_by(remote_node_id=node_id)
            .order_by(RemoteFavorite.created_at.desc()).all())
    return [rid for (rid,) in rows]


@bp.route("/<int:node_id>/ids")
@login_required
def list_ids(node_id):
    """Ids only — no network. The star on a recording page paints from this, so
    it must not depend on the remote being reachable to know whether I starred
    something."""
    node, err = _node_or_404(node_id)
    if err:
        return err
    return jsonify(_favorite_ids(node_id))


@bp.route("/<int:node_id>")
@login_required
def list_favorites(node_id):
    """Displayable rows, resolved from the remote in ONE batched call.

    Nothing about the recording is stored locally (see
    models/remote_favorite.py), so this is where bare ids become performer,
    date and venue. If the library is unreachable the error travels — a
    favourites list that silently renders empty when someone's node is offline
    is the empty-vs-broken confusion this project keeps relearning.
    """
    node, err = _node_or_404(node_id)
    if err:
        return err

    ids = _favorite_ids(node_id)
    if not ids:
        return jsonify([])

    card = "1" if request.args.get("card", "").lower() in ("1", "true", "yes") else ""
    query = "ids=" + ",".join(str(i) for i in ids)
    if card:
        query += "&card=1"

    payload, err = fetch_remote_json(node, "recordings/by-ids", query)
    if err:
        return err

    # Preserve MY ordering (newest star first). The remote answered in its own
    # order and has no idea when I starred anything.
    by_id = {r["id"]: r for r in payload if isinstance(r, dict) and "id" in r}
    return jsonify([by_id[i] for i in ids if i in by_id])


@bp.route("/<int:node_id>", methods=["POST"])
@login_required
def add_favorite(node_id):
    node, err = _node_or_404(node_id)
    if err:
        return err

    rid = (request.get_json(silent=True) or {}).get("recording_id")
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return jsonify({"error": "recording_id is required"}), 400

    # Deliberately NOT verified against the remote first. The unique constraint
    # makes a double-star harmless, and a round trip to confirm a row I am
    # looking at right now would make the star feel slow for no safety gained —
    # an id I cannot see simply resolves to nothing when the list is rendered.
    exists = (db.session.query(RemoteFavorite)
              .filter_by(remote_node_id=node_id, remote_recording_id=rid).first())
    if not exists:
        db.session.add(RemoteFavorite(remote_node_id=node_id,
                                      remote_recording_id=rid))
        db.session.commit()
    return jsonify({"ok": True, "recording_id": rid, "is_favorite": True}), 201


@bp.route("/<int:node_id>/<int:recording_id>", methods=["DELETE"])
@login_required
def remove_favorite(node_id, recording_id):
    row = (db.session.query(RemoteFavorite)
           .filter_by(remote_node_id=node_id, remote_recording_id=recording_id).first())
    if row:
        db.session.delete(row)
        db.session.commit()
    return jsonify({"ok": True, "recording_id": recording_id, "is_favorite": False})
