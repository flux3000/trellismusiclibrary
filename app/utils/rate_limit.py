"""
app/utils/rate_limit.py — a small in-process sliding-window limiter.

Built 2026-08-25 for `/api/share/enroll`, which is the ONLY route on the peer
door reachable with no credentials at all — a peer holding an invite has no
token yet, so the invite code has to be presentable by a stranger. Everything
else on that door demands a bearer token before it does any work.

Deliberately NOT a dependency
-----------------------------
flask-limiter is the obvious package and it was considered. It brings a storage
backend abstraction and a configuration language for a problem that, here, is
one route and one rule. A single process serving one household's library does
not need shared state across workers, and an in-process dict has no failure mode
where the limiter itself takes the app down. If the day comes that several
processes must share a count, revisit this — it is thirty lines to throw away.

Identifying the caller correctly matters more than the algorithm
---------------------------------------------------------------
Behind a Cloudflare Tunnel, `cloudflared` runs on this same machine and connects
to 127.0.0.1, so `request.remote_addr` is the LOOPBACK ADDRESS for every visitor
on earth. A naive IP limiter would therefore put the entire internet in one
bucket, and the first bot to trip the limit would lock out every real peer.

The real address arrives in a header the proxy adds (`CF-Connecting-IP` for
Cloudflare). A header is spoofable by whoever is talking to us — so it is
trusted only when the immediate peer is loopback, which behind a tunnel is the
only thing that can reach us anyway, and never for a caller arriving over the
LAN. Unset by default: an unconfigured install must not trust a header it was
never told to expect.
"""

import ipaddress
import threading
import time
from collections import deque
from functools import wraps

from flask import request, jsonify, current_app

# key -> deque[float timestamps], newest last.
_hits = {}
_lock = threading.Lock()

# Stop an attacker from growing the dict without bound by varying their key.
# Well past any plausible number of real peers; when exceeded we drop the
# least recently touched buckets rather than refusing service.
_MAX_KEYS = 4096


def _is_loopback(addr):
    if not addr:
        return False
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def client_key():
    """
    Who is asking, as well as we can tell.

    Returns the forwarded address when this install has been told to expect a
    proxy AND the immediate peer is loopback; otherwise the socket address.
    Falls back to a fixed string so a request with no address at all still lands
    in *some* bucket rather than escaping the limiter entirely.
    """
    header = current_app.config.get("TRUSTED_CLIENT_IP_HEADER")
    if header and _is_loopback(request.remote_addr):
        forwarded = (request.headers.get(header) or "").strip()
        if forwarded:
            # A proxy may append; the first entry is the originating client.
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _prune(bucket, now, window):
    while bucket and (now - bucket[0]) >= window:
        bucket.popleft()


def check(key, limit, window):
    """
    Record an attempt and say whether it is allowed.

    Returns (allowed, retry_after_seconds). retry_after is 0 when allowed. A
    REFUSED attempt is NOT recorded — otherwise a caller who keeps hammering
    permanently re-arms their own block and can never come back, which turns a
    speed bump into a lifetime ban earned by a script they forgot to stop.
    """
    now = time.monotonic()
    with _lock:
        bucket = _hits.get(key)
        if bucket is None:
            if len(_hits) >= _MAX_KEYS:
                _evict_locked(now, window)
            bucket = _hits[key] = deque()

        _prune(bucket, now, window)

        if len(bucket) >= limit:
            retry = max(1, int(window - (now - bucket[0])) + 1)
            return False, retry

        bucket.append(now)
        return True, 0


def _evict_locked(now, window):
    """Drop buckets with nothing live in them; if that frees nothing, drop the
    oldest half. Called with the lock held."""
    for key in [k for k, b in _hits.items()
                if not b or (now - b[-1]) >= window]:
        del _hits[key]
    if len(_hits) >= _MAX_KEYS:
        for key in sorted(_hits, key=lambda k: _hits[k][-1])[:len(_hits) // 2]:
            del _hits[key]


def reset():
    """Clear all state. For tests — never called by the app."""
    with _lock:
        _hits.clear()


def rate_limited(limit_key, window_key, bucket="default"):
    """
    Limit a view, reading the numbers from config at REQUEST time so a test (or
    an operator) can change them without re-importing the module.

    Setting the limit to 0 or less disables the check — an explicit escape
    hatch, so nobody has to comment out a decorator to run a load test.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            limit = current_app.config.get(limit_key, 0)
            window = current_app.config.get(window_key, 0)
            if limit and limit > 0 and window and window > 0:
                allowed, retry = check(f"{bucket}:{client_key()}", limit, window)
                if not allowed:
                    resp = jsonify({
                        "error": "Too many attempts. Try again shortly.",
                        "code":  "rate_limited",
                    })
                    resp.status_code = 429
                    resp.headers["Retry-After"] = str(retry)
                    return resp
            return fn(*args, **kwargs)
        return wrapper
    return decorator
