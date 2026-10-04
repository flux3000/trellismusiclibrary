"""
app/utils/resolver_corpus/polite.py -- the one HTTP door for the corpus collectors.

Rules, enforced here so neither collector can forget them:
  * at most one request per `min_interval` seconds (default 2), plus jitter;
  * Retry-After is honoured; 429 and 5xx back off and retry;
  * 403/401, or a bot-protection challenge page, STOPS the run (BotProtection).
    Nothing here tries to get around it;
  * every answer is cached on disk by URL (including 404s), so a rerun costs
    nothing for what it already has -- that is what makes a run resumable;
  * a hard cap on network requests (`max_requests`);
  * audio is refused outright, and bodies are read only up to `max_bytes`.

Clock, sleep, randomness and the transport are injectable so the rules are
unit-tested without a network or real waiting.
"""
import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

AUDIO_EXTS = (".flac", ".shn", ".mp3", ".wav", ".aif", ".aiff", ".ogg", ".m4a",
              ".ape", ".wv", ".wma", ".aac", ".opus", ".mp4", ".mpg", ".mov",
              ".avi", ".st5", ".zip", ".rar", ".7z", ".gz", ".tar", ".iso", ".torrent")
ALLOWED_HOSTS = ("bluegrassarchive.com", "gdarchive.net", "archive.org")
SAFETY_MAX_BYTES = 64 * 1024 * 1024
CHALLENGE_MARKERS = (b"just a moment...", b"cf-chl-", b"attention required! | cloudflare",
                     b"checking your browser before accessing")


class FetchError(Exception):
    """A request failed after its retries; the caller may skip the item."""


class BotProtection(Exception):
    """The site refused us (403/401 or a challenge page). The run must stop."""


class RequestCap(Exception):
    """The hard cap on network requests was reached."""


@dataclass
class Fetched:
    url: str
    status: int            # 200, or 404/410 (cached negative)
    body: bytes = b""
    from_cache: bool = False
    too_large: bool = False

    @property
    def ok(self):
        return self.status == 200 and not self.too_large


def _default_opener(url, headers, timeout, max_bytes):
    """-> (status, headers, body). Never raises for an HTTP status."""
    req = urllib.request.Request(url, headers=headers)
    from app.utils.net import SSL_CONTEXT
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as r:
            body = r.read(max_bytes + 1)
            return r.status, dict(r.headers.items()), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()) if e.headers else {}, b""


def retry_after_seconds(headers, wall=time.time):
    """Retry-After as seconds (delta or HTTP-date), or None."""
    value = None
    for k, v in (headers or {}).items():
        if k.lower() == "retry-after":
            value = str(v).strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - wall())
    except (TypeError, ValueError):
        return None


class PoliteFetcher:
    def __init__(self, cache_dir, *, min_interval=2.0, jitter=0.5, clock=time.monotonic,
                 sleep=time.sleep, rng=random.random, wall=time.time, opener=None,
                 max_requests=None, max_retries=5, backoff_base=30.0, backoff_cap=600.0,
                 user_agent=None, timeout=30, refresh=False, hosts=ALLOWED_HOSTS):
        if user_agent is None:
            from app.utils.net import USER_AGENT
            user_agent = USER_AGENT
        self.cache_dir = Path(cache_dir)
        self.min_interval, self.jitter = float(min_interval), float(jitter)
        self._clock, self._sleep, self._rng, self._wall = clock, sleep, rng, wall
        self._opener = opener or _default_opener
        self.max_requests, self.max_retries = max_requests, max_retries
        self.backoff_base, self.backoff_cap = backoff_base, backoff_cap
        self.headers = {"User-Agent": user_agent, "Accept": "*/*"}
        self.timeout, self.refresh, self.hosts = timeout, refresh, tuple(hosts)
        self._next_ok = None
        self.requests = 0          # network requests made (retries count)
        self.cache_hits = 0
        self.waited = 0.0

    # -- cache ---------------------------------------------------------------
    def _paths(self, url):
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()
        d = self.cache_dir / h[:2]
        return d / f"{h}.json", d / f"{h}.bin"

    def is_cached(self, url):
        return (not self.refresh) and self._paths(url)[0].exists()

    def _read_cache(self, url):
        meta_p, body_p = self._paths(url)
        if self.refresh or not meta_p.exists():
            return None
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            body = body_p.read_bytes() if body_p.exists() else b""
        except (OSError, ValueError):
            return None
        return Fetched(url, int(meta.get("status", 0)), body, True, bool(meta.get("too_large")))

    def _write_cache(self, url, fetched):
        meta_p, body_p = self._paths(url)
        meta_p.parent.mkdir(parents=True, exist_ok=True)
        if fetched.body:
            tmp = body_p.with_suffix(".bin.tmp")
            tmp.write_bytes(fetched.body)
            os.replace(tmp, body_p)
        elif body_p.exists():
            body_p.unlink()
        meta = {"url": url, "status": fetched.status, "too_large": fetched.too_large,
                "bytes": len(fetched.body)}
        tmp = meta_p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(tmp, meta_p)          # meta last: its presence means the entry is whole

    def cache_size(self):
        return sum(1 for _ in self.cache_dir.glob("*/*.json")) if self.cache_dir.exists() else 0

    # -- pacing --------------------------------------------------------------
    def _pace(self):
        now = self._clock()
        if self._next_ok is not None and now < self._next_ok:
            wait = self._next_ok - now
            self.waited += wait
            self._sleep(wait)
            now = self._clock()
        self._next_ok = now + self.min_interval + self.jitter * self._rng()

    def _backoff(self, seconds):
        self.waited += seconds
        self._sleep(seconds)
        # the wait counts toward the next interval; pacing continues from here
        self._next_ok = self._clock() + self.min_interval

    # -- guards --------------------------------------------------------------
    def check_url(self, url):
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https"):
            raise ValueError(f"refusing non-http URL: {url}")
        host = (p.hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in self.hosts):
            raise ValueError(f"refusing host outside the allow-list: {host}")
        if urllib.parse.unquote(p.path).lower().endswith(AUDIO_EXTS):
            raise ValueError(f"refusing to fetch audio or an archive: {url}")

    # -- the one entry point ---------------------------------------------------
    def get(self, url, max_bytes=None):
        self.check_url(url)
        cached = self._read_cache(url)
        if cached is not None:
            self.cache_hits += 1
            return cached
        limit = int(max_bytes or SAFETY_MAX_BYTES)
        attempt = 0
        while True:
            if self.max_requests is not None and self.requests >= self.max_requests:
                raise RequestCap(f"request cap {self.max_requests} reached")
            self._pace()
            self.requests += 1
            try:
                status, headers, body = self._opener(url, self.headers, self.timeout, limit)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                status, headers, body = None, {}, b""
                err = f"{type(e).__name__}: {e}"
            else:
                err = None
            if status == 200:
                if len(body) > limit:
                    out = Fetched(url, 200, b"", False, True)      # oversize: remembered, not kept
                else:
                    low = body[:4096].lower()
                    if any(m in low for m in CHALLENGE_MARKERS):
                        raise BotProtection(f"challenge page from {url}; stopping")
                    out = Fetched(url, 200, body)
                self._write_cache(url, out)
                return out
            if status in (404, 410):
                out = Fetched(url, status)
                self._write_cache(url, out)
                return out
            if status in (401, 403):
                raise BotProtection(f"HTTP {status} from {url}; stopping, not going around it")
            if status is None or status == 429 or status >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise FetchError(f"{url}: gave up after {self.max_retries} retries ({err or status})")
                ra = retry_after_seconds(headers, self._wall)
                wait = ra if ra is not None else min(self.backoff_cap, self.backoff_base * 2 ** (attempt - 1))
                self._backoff(min(wait, 3600.0))
                continue
            raise FetchError(f"{url}: HTTP {status}")
