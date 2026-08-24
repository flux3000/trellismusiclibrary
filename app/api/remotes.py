"""
api/remotes.py — OUTBOUND sharing: consuming someone else's Flux library.

The mirror of api/peers.py. That file manages who may consume MY library; this
one manages whose libraries I consume, and relays my requests to them.

Routes (url_prefix /api/remotes):
  POST   /enroll              { invite } or { invite_code, base_url } → join
  GET    /                    list joined libraries
  DELETE /<id>                leave (drops the row's keychain token)
  GET    /<id>/<subpath>      PROXY → GET {base_url}/api/share/<subpath>

Why proxy at all
----------------
The token never touches the browser. The PyWebView frontend talks only to its
own localhost Flask; localhost Flask holds the credential and relays. One
security model in the webview, no CORS, and a token that cannot be read out of
devtools or a saved page.

Why ONE generic route instead of a route per endpoint
-----------------------------------------------------
The share API is twelve endpoints and will grow. Twelve hand-written proxy
stubs would need editing every time it does, and each is a chance to forget
the token or the error mapping. The remote enforces its own authorization on
every call — this side's job is transport, not policy.

The allowlist is what keeps "generic" from meaning "open relay": without it,
this endpoint would fetch arbitrary paths from an arbitrary host on behalf of
anyone with a local session.
"""

import json as _json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, Response, current_app

from app.extensions import db
from app.models.remote_node import RemoteNode
from app.utils.prefs import get_remote_token, set_remote_token, delete_remote_token
from app.api.peers import admin_required          # decorator, not a view

bp = Blueprint("remotes", __name__)

# First path segment must be one of these. Mirrors api/share.py's surface;
# anything not listed is refused here rather than sent to the remote.
# ⚠ THIS LIST, api.js's REMOTE_CAPABLE, AND share.py's ROUTES MUST AGREE.
# Three lists, three failure modes, all silent: a path missing HERE is refused
# by the proxy; missing from REMOTE_CAPABLE it is never rewritten and quietly
# queries the VIEWER's own library instead (which is how peer search returned
# nothing for a whole afternoon — the consumer's library is empty, so an empty
# result looked like "no matches"); missing from share.py it 404s and renders
# as an empty page. tests/test_peer_surface_parity.py asserts all three.
_ALLOWED_PREFIXES = {
    "me", "collections", "recordings", "performances", "performers",
    "venues", "artists", "genres", "stream", "search",
}

# Header names worth carrying in each direction. Everything else is dropped —
# a proxy that forwards headers it doesn't understand is a way to leak local
# session state to a third party.
_FORWARD_REQUEST_HEADERS = ("Range",)
_COPY_RESPONSE_HEADERS = (
    "Content-Type", "Content-Length", "Content-Range",
    "Accept-Ranges", "Cache-Control",
)

_CHUNK = 64 * 1024
_TIMEOUT = 30


def _utcnow():
    return datetime.now(timezone.utc)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects when talking to a remote.

    A remote that redirects us is a remote whose auth failed (or whose address
    is wrong). Following it can turn a 401 into a 200 page of HTML — exactly
    the trap the local door used to set, and the reason the peer-door probe
    once reported a breach that wasn't. The status the remote actually
    returned is the only useful signal.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def fetch_remote_json(node, subpath, query=None):
    """GET a share-door path on `node` and return (payload, error_response).

    Extracted so server-side callers can reach a remote without going through
    the HTTP proxy route — api/remote_favorites.py needs to resolve a list of
    bare recording ids into rows, and doing that by having the browser call the
    proxy would mean the frontend orchestrating a fan-out it has no reason to
    know about.

    Returns exactly one of the two: on success `(payload, None)`, on failure
    `(None, flask_response)` carrying the same distinctions the proxy draws —
    401 "no longer recognises you" is not 403 "not shared with you" is not 502
    "could not reach". Collapsing those is how "they revoked me" and "my wifi
    is down" become the same empty list.
    """
    token = get_remote_token(node.id)
    if not token:
        return None, (jsonify({
            "error": "No stored access token for this library. It may need to "
                     "be joined again, or the OS keychain may be unavailable."
        }), 409)

    url = f"{node.base_url}/api/share/{subpath}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")

    try:
        with _opener.open(req, timeout=_TIMEOUT) as resp:
            payload = _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = None
        try:
            detail = _json.loads(e.read()).get("error")
        except Exception:
            pass
        if e.code == 401:
            return None, (jsonify({"error": "This library no longer recognises "
                                            "your access.", "remote_status": 401}), 401)
        return None, (jsonify({"error": detail or f"The library returned {e.code}",
                               "remote_status": e.code}), 502)
    except urllib.error.URLError as e:
        return None, (jsonify({"error": f"Could not reach {node.display_name}: "
                                        f"{e.reason}"}), 502)
    except ValueError:
        return None, (jsonify({"error": "The library sent something unreadable"}), 502)

    node.last_connected_at = _utcnow()
    db.session.commit()
    return _rewrite_share_urls(payload, node.id), None


def _serialize(node):
    return {
        "id":                node.id,
        "display_name":      node.display_name,
        "base_url":          node.base_url,
        "owner_name":        node.owner_name,
        "peer_name":         node.peer_name,
        "enrolled_at":       node.enrolled_at.isoformat() if node.enrolled_at else None,
        "last_connected_at": (node.last_connected_at.isoformat()
                              if node.last_connected_at else None),
        "is_active":         node.is_active,
        # Whether the credential is actually retrievable. A remote whose token
        # cannot be read must present as broken, never as an empty library —
        # "they revoked me" and "my keychain is locked" look identical
        # otherwise, and only one is the user's problem.
        "has_token":         get_remote_token(node.id) is not None,
    }


def _normalise_base_url(raw):
    """Strip trailing slashes so path joining can't produce '//'."""
    return (raw or "").strip().rstrip("/")


# ── POST /api/remotes/enroll ──────────────────────────────────────────────────
# Accepts either the single compound string the design specifies
# ("https://their-box#CODE") or the two fields separately.

@bp.route("/enroll", methods=["POST"])
@admin_required
def enroll():
    data = request.get_json(silent=True) or {}

    invite = (data.get("invite") or "").strip()
    if invite:
        # rsplit: an address may legitimately contain no '#', but the code
        # never does, so the LAST '#' is always the separator.
        if "#" not in invite:
            return jsonify({"error": "Invite must look like https://their-address#CODE"}), 400
        base_url, _, code = invite.rpartition("#")
    else:
        base_url = data.get("base_url") or ""
        code = data.get("invite_code") or ""

    base_url = _normalise_base_url(base_url)
    code = (code or "").strip()
    if not base_url or not code:
        return jsonify({"error": "Both an address and an invite code are required"}), 400

    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return jsonify({"error": f"Not a usable address: {base_url}"}), 400

    existing = (db.session.query(RemoteNode)
                .filter_by(base_url=base_url, left_at=None).first())
    if existing:
        return jsonify({"error": f"Already joined {existing.display_name}"}), 409

    body = _json.dumps({
        "invite_code": code,
        # The label the REMOTE files this device under ("Matt's MacBook").
        # Falls back to the product name only when this node has none of its
        # own; "Flux" here was stale branding.
        "device_label": current_app.config.get("SHARE_NODE_NAME") or "Trellis",
    }).encode()
    req = urllib.request.Request(f"{base_url}/api/share/enroll", data=body, method="POST")
    req.add_header("Content-Type", "application/json")

    try:
        with _opener.open(req, timeout=_TIMEOUT) as resp:
            payload = _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            detail = _json.loads(e.read()).get("error")
        except Exception:
            detail = None
        # Pass the remote's own refusal through rather than inventing one —
        # "Invalid or expired invite" is the useful message here.
        return jsonify({"error": detail or f"The library refused the invite ({e.code})"}), 400
    except urllib.error.URLError as e:
        return jsonify({"error": f"Could not reach {base_url}: {e.reason}"}), 502
    except ValueError:
        return jsonify({"error": "The address answered, but not like a Flux library"}), 502

    token = payload.get("token")
    if not token:
        return jsonify({"error": "The library did not issue a token"}), 502

    node = RemoteNode(
        display_name=payload.get("node_name") or base_url,
        base_url=base_url,
        owner_name=payload.get("owner_name"),
        peer_name=payload.get("peer_name"),
        last_connected_at=_utcnow(),
    )
    db.session.add(node)
    db.session.flush()          # need the id to key the keychain entry

    try:
        set_remote_token(node.id, token)
    except RuntimeError as e:
        # No keychain, no enrollment. Committing the row without storing the
        # token would leave a library that can never be opened and an invite
        # that has already been consumed — worse than failing here.
        db.session.rollback()
        return jsonify({"error": f"Could not store the access token: {e}"}), 500

    db.session.commit()
    return jsonify(_serialize(node)), 201


# ── GET /api/remotes/ ─────────────────────────────────────────────────────────

@bp.route("/")
@admin_required
def list_remotes():
    nodes = (db.session.query(RemoteNode)
             .filter_by(left_at=None)
             .order_by(RemoteNode.display_name).all())
    return jsonify([_serialize(n) for n in nodes])


# ── DELETE /api/remotes/<id> ──────────────────────────────────────────────────

@bp.route("/<int:node_id>", methods=["DELETE"])
@admin_required
def leave_remote(node_id):
    node = db.session.get(RemoteNode, node_id)
    if not node:
        return jsonify({"error": "Not found"}), 404
    # Drop the credential first. If the commit failed after deleting the row we
    # would orphan a live token in the keychain with nothing pointing at it.
    delete_remote_token(node.id)
    node.left_at = _utcnow()
    db.session.commit()
    return jsonify({"status": "left", "id": node.id})


# ── GET /api/remotes/<id>/<subpath> — the proxy ───────────────────────────────

def _rewrite_share_urls(obj, node_id):
    """
    Rewrite every '/api/share/...' string in a payload to '/api/remotes/<id>/...'.

    The remote hands out paths on ITS box — stream_url is '/api/share/stream/12',
    image urls are '/api/share/performers/images/4'. Handed to the frontend
    unchanged, those resolve against localhost and 404, because localhost has no
    token-authenticated share door of its own.

    Done here, once, rather than at each call site: the alternative is every
    consumer of every payload remembering to rewrite, and the failure mode of
    forgetting (a silent 404 on a photo, a player that won't start) is quiet.
    """
    if isinstance(obj, str):
        if obj.startswith("/api/share/"):
            return f"/api/remotes/{node_id}/" + obj[len("/api/share/"):]
        return obj
    if isinstance(obj, list):
        return [_rewrite_share_urls(v, node_id) for v in obj]
    if isinstance(obj, dict):
        return {k: _rewrite_share_urls(v, node_id) for k, v in obj.items()}
    return obj


@bp.route("/<int:node_id>/<path:subpath>")
@admin_required
def proxy(node_id, subpath):
    node = db.session.get(RemoteNode, node_id)
    if not node or not node.is_active:
        return jsonify({"error": "Not found"}), 404

    first = subpath.split("/", 1)[0]
    if first not in _ALLOWED_PREFIXES:
        # Not merely unknown — refused. This is what stops a local session from
        # using the app as a relay to arbitrary paths on a remote host.
        return jsonify({"error": f"Not a shareable path: {first}"}), 403

    token = get_remote_token(node.id)
    if not token:
        return jsonify({
            "error": "No stored access token for this library. It may need to be "
                     "joined again, or the OS keychain may be unavailable."
        }), 409

    url = f"{node.base_url}/api/share/{subpath}"
    if request.query_string:
        url = f"{url}?{request.query_string.decode()}"

    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    for header in _FORWARD_REQUEST_HEADERS:
        if header in request.headers:
            req.add_header(header, request.headers[header])

    try:
        resp = _opener.open(req, timeout=_TIMEOUT)
    except urllib.error.HTTPError as e:
        detail = None
        try:
            detail = _json.loads(e.read()).get("error")
        except Exception:
            pass
        if e.code == 401:
            # Their door rejected our token: revoked, or the node was rebuilt.
            # Distinct message because the remedy is different from a 403.
            return jsonify({"error": "This library no longer recognises your access.",
                            "remote_status": 401}), 401
        if e.code == 403:
            return jsonify({"error": detail or "Not shared with you.",
                            "remote_status": 403}), 403
        return jsonify({"error": detail or f"The library returned {e.code}",
                        "remote_status": e.code}), 502
    except urllib.error.URLError as e:
        return jsonify({"error": f"Could not reach {node.display_name}: {e.reason}"}), 502

    node.last_connected_at = _utcnow()
    db.session.commit()

    ctype = resp.headers.get("Content-Type", "")

    # JSON: read it whole so share URLs can be rewritten before it reaches the
    # frontend. These payloads are small — a collection listing, a performer.
    if "application/json" in ctype:
        try:
            payload = _json.loads(resp.read())
        except ValueError:
            return jsonify({"error": "The library sent something unreadable"}), 502
        finally:
            resp.close()
        return jsonify(_rewrite_share_urls(payload, node.id)), resp.status

    # Everything else — audio, images — is streamed straight through. Never
    # buffered: a transcoded show is tens of megabytes and holding it in memory
    # to hand it to a player that wants it in chunks would be absurd.
    def _stream():
        try:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            resp.close()

    out = Response(_stream(), status=resp.status)
    for header in _COPY_RESPONSE_HEADERS:
        value = resp.headers.get(header)
        if value:
            out.headers[header] = value
    return out
