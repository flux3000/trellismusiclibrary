"""
tests/test_peer_surface_parity.py — the three lists that must agree.

WHY THIS FILE EXISTS

A path is only reachable by a peer if THREE independent places allow it:

  1. `app/api/share.py`            — the door actually serves it
  2. `_ALLOWED_PREFIXES` in
     `app/api/remotes.py`          — the local proxy will relay it
  3. `REMOTE_CAPABLE` in
     `app/static/js/api.js`        — the client rewrites the request at all

Every combination of "missing from one of these" fails SILENTLY, and each one
fails differently, which is why they kept being diagnosed as data problems:

  * missing from share.py      → 404, which renders as an empty page
                                 ("Failed to load library", "None yet")
  * missing from the allowlist → the proxy refuses; also an empty page
  * missing from REMOTE_CAPABLE → the request is NEVER REWRITTEN and quietly
                                 queries the VIEWER'S OWN library. On a
                                 listener's node that library is empty, so the
                                 answer is a perfectly valid empty result. This
                                 is the worst of the three: nothing errors
                                 anywhere. Peer search returned nothing for an
                                 afternoon on 2026-08-24 for exactly this
                                 reason.

So the lists are asserted against each other rather than trusted. Adding an
endpoint to share.py without wiring the other two now fails here instead of in
front of a user.
"""

import io
import re
from pathlib import Path

REPO = Path(__file__).parent.parent

# Served on the share door but never proxied: `enroll` is hit DIRECTLY on the
# remote host (it is how you get a token in the first place, so there is no
# token yet to proxy with), and `me` is called by the proxy itself rather than
# by a client path.
NOT_CLIENT_ROUTED = {"enroll"}
NOT_CLIENT_REWRITTEN = {"enroll", "me"}


def _share_prefixes():
    """First path segment of every route declared in api/share.py."""
    src = io.open(REPO / "app" / "api" / "share.py", encoding="utf-8").read()
    out = set()
    for m in re.finditer(r'@bp\.route\(\s*["\']/([^"\'/<]+)', src):
        out.add(m.group(1))
    return out


def _proxy_allowlist():
    src = io.open(REPO / "app" / "api" / "remotes.py", encoding="utf-8").read()
    block = re.search(r"_ALLOWED_PREFIXES\s*=\s*\{(.*?)\}", src, re.S).group(1)
    return set(re.findall(r'"([a-z-]+)"', block))


def _client_remote_capable():
    src = io.open(REPO / "app" / "static" / "js" / "api.js", encoding="utf-8").read()
    block = re.search(r"REMOTE_CAPABLE\s*=\s*new Set\(\[(.*?)\]\)", src, re.S).group(1)
    return set(re.findall(r"'([a-z-]+)'", block))


def test_share_routes_are_all_proxyable():
    missing = _share_prefixes() - NOT_CLIENT_ROUTED - _proxy_allowlist()
    assert not missing, (
        f"share.py serves {sorted(missing)} but _ALLOWED_PREFIXES in "
        f"api/remotes.py will not relay it — the proxy refuses and the peer "
        f"sees an empty page.")


def test_share_routes_are_all_rewritten_by_the_client():
    missing = _share_prefixes() - NOT_CLIENT_REWRITTEN - _client_remote_capable()
    assert not missing, (
        f"share.py serves {sorted(missing)} but REMOTE_CAPABLE in "
        f"static/js/api.js does not rewrite it. The request will silently query "
        f"the VIEWER'S OWN library instead of the remote — which on a "
        f"listener's empty node looks exactly like 'no results'.")


def test_client_does_not_rewrite_paths_the_door_cannot_serve():
    """The mirror failure: rewriting a path share.py has no route for turns a
    working local page into a 404 the moment a remote library is selected."""
    extra = _client_remote_capable() - _share_prefixes()
    assert not extra, (
        f"REMOTE_CAPABLE rewrites {sorted(extra)} but share.py has no such "
        f"route — selecting a remote library will 404 that page.")


def test_proxy_allowlist_has_no_dead_entries():
    extra = _proxy_allowlist() - _share_prefixes()
    assert not extra, (
        f"_ALLOWED_PREFIXES permits {sorted(extra)} with no matching route on "
        f"the share door. An allowlist entry with nothing behind it is a "
        f"widened proxy surface bought for nothing.")
