"""
app/sources/lma.py -- Internet Archive Live Music Archive (collection `etree`).

Lists come from advancedsearch.php, one item's detail from /metadata/<id>.
NOT the Changes API (Ryan, 2026-10-01): a browsable catalog needs sorting and
paging, which the change feed does not give.

Everything goes through app/utils/net.py (certifi context, shared User-Agent).
Under TESTING the default HTTP functions refuse, same rule as musicbrainz.py:
a unit test must never depend on a round-trip to archive.org. Tests pass fakes
to LmaSource() or register_source("lma", fake).
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app

from app.utils.net import SSL_CONTEXT, USER_AGENT
from .base import Source, SourceError, SourceNotFound

_ROWS = 50
_LIST_TTL = 300      # 5 minutes
_ITEM_TTL = 600      # 10 minutes
_TIMEOUT = 20

_SORTS = {
    "newest": "addeddate desc",
    "date":   "date desc",
    "az":     "creatorSorter asc",
}

_LIST_FIELDS = ("identifier", "creator", "date", "venue", "coverage", "source",
                "format", "item_size", "addeddate", "collection")

_AUDIO_EXT = (".flac", ".shn", ".mp3", ".wav", ".aif", ".aiff", ".ogg", ".m4a", ".ape", ".wv")
# Metadata files the archive marks `original` on some items but that are not
# the show's own paperwork.
_SKIP_SUFFIX = ("_meta.xml", "_files.xml", "_reviews.xml", "_meta.sqlite")

# Lucene specials. `&` and `|` are only special doubled, but escaping each one
# is harmless and simpler than detecting the pair.
_LUCENE_SPECIALS = set('+-&|!(){}[]^"~*?:\\/')


def escape_lucene(text):
    return "".join("\\" + c if c in _LUCENE_SPECIALS else c for c in text)


def build_search_query(q):
    """
    Plain text in, Lucene out. Each word must match creator, venue or title;
    a bare YYYY or YYYY-MM-DD word matches the date instead. Words are ANDed.
    """
    parts = ["collection:etree"]
    for tok in (q or "").split():
        if re.fullmatch(r"\d{4}", tok):
            parts.append(f"date:[{tok}-01-01 TO {tok}-12-31]")
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", tok):
            parts.append(f"date:[{tok} TO {tok}]")
        else:
            if tok.upper() in ("AND", "OR", "NOT", "TO"):
                esc = f'"{tok}"'
            else:
                esc = escape_lucene(tok)
            parts.append(f"(creator:{esc} OR venue:{esc} OR title:{esc})")
    return " AND ".join(parts)


def _as_list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _first(v):
    lst = _as_list(v)
    return str(lst[0]) if lst else ""


def _truthy(v):
    return str(_first(v)).strip().lower() in ("true", "1", "yes")


def _to_int(v, default=0):
    try:
        return int(float(str(_first(v))))
    except (TypeError, ValueError):
        return default


def parse_source_type(source_text, identifier=""):
    """SBD, AUD, MTX, FM or None. The identifier's dotted token (gd77-05-08.sbd.x)
    is the taper community's own label and wins over free text; in free text a
    matrix beats a soundboard beats a broadcast beats an audience mention,
    because a matrix's source line names its ingredients."""
    ident_tokens = {t.lower() for t in re.split(r"[^A-Za-z0-9]+", identifier or "")}
    for label, keys in (("MTX", ("mtx", "matrix")), ("SBD", ("sbd", "soundboard")),
                        ("FM", ("fm",)), ("AUD", ("aud", "audience"))):
        if ident_tokens & set(keys):
            return label
    text = (source_text or "").lower()
    for label, pat in (("MTX", r"\b(mtx|matrix)\b"),
                       ("SBD", r"\b(sbd|soundboard|board)\b"),
                       ("FM", r"\b(fm|broadcast|radio)\b"),
                       ("AUD", r"\b(aud|audience)\b")):
        if re.search(pat, text):
            return label
    return None


def parse_format(formats):
    """Flac -> FLAC, Shorten -> SHN, else MP3 when only MP3, else None."""
    fl = [str(f).strip().lower() for f in _as_list(formats)]
    if any(f in ("flac", "24bit flac") for f in fl):
        return "FLAC"
    if any("shorten" in f for f in fl):
        return "SHN"
    if any("mp3" in f for f in fl):
        return "MP3"
    return None


def _date10(v):
    s = _first(v)
    m = re.match(r"\d{4}(-\d{2}(-\d{2})?)?", s)
    return m.group(0) if m else ""


def _length_s(v):
    s = str(_first(v)).strip()
    if not s:
        return None
    if ":" in s:
        try:
            secs = 0.0
            for p in s.split(":"):
                secs = secs * 60 + float(p)
            return int(round(secs))
        except ValueError:
            return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def _is_audio(name):
    return name.lower().endswith(_AUDIO_EXT)


def _public_originals(files):
    out = []
    for f in files or []:
        if str(f.get("source", "")).lower() != "original":
            continue
        if _truthy(f.get("private")):
            continue
        name = f.get("name") or ""
        if not name or name.lower().endswith(_SKIP_SUFFIX):
            continue
        out.append(f)
    return out


def _http_get(url, timeout=_TIMEOUT):
    # Network is off under TESTING, same rule as musicbrainz.enabled().
    try:
        if current_app.config.get("TESTING"):
            raise SourceError("Network disabled under TESTING")
    except RuntimeError:
        pass
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SourceNotFound("Not found") from e
        raise SourceError(f"Archive answered {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise SourceError("Archive could not be reached") from e


def _default_get_json(url):
    try:
        return json.loads(_http_get(url).decode("utf-8", "replace"))
    except ValueError as e:
        raise SourceError("Archive sent an unreadable answer") from e


def _default_get_text(url, limit=200_000):
    return _http_get(url)[:limit].decode("utf-8", "replace")


class LmaSource(Source):
    name = "lma"

    def __init__(self, get_json=None, get_text=None, clock=time.time):
        self._get_json = get_json or _default_get_json
        self._get_text = get_text or _default_get_text
        self._clock = clock
        self._lock = threading.Lock()
        self._cache = {}     # key -> (expires, value)

    # -- cache ---------------------------------------------------------------
    def _cached(self, key, ttl, loader):
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = loader()
        with self._lock:
            if len(self._cache) > 500:
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            self._cache[key] = (now + ttl, value)
        return value

    # -- lists ---------------------------------------------------------------
    def _list(self, query, page, sort):
        sort_expr = _SORTS.get(sort, _SORTS["newest"])
        page = max(1, int(page or 1))

        def load():
            params = [("q", query)]
            params += [("fl[]", f) for f in _LIST_FIELDS]
            params += [("sort[]", sort_expr), ("rows", str(_ROWS)),
                       ("page", str(page)), ("output", "json")]
            url = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params)
            data = self._get_json(url)
            resp = data.get("response") or {}
            docs = [self._item_from_doc(d) for d in resp.get("docs", [])]
            found = _to_int(resp.get("numFound"))
            return docs, page * _ROWS < found

        return self._cached(("list", query, page, sort_expr), _LIST_TTL, load)

    def recent(self, page=1, sort="newest"):
        return self._list("collection:etree", page, sort)

    def search(self, q, page=1, sort="newest"):
        return self._list(build_search_query(q), page, sort)

    def _item_from_doc(self, d):
        ident = _first(d.get("identifier"))
        src_text = _first(d.get("source"))
        colls = [str(c).lower() for c in _as_list(d.get("collection"))]
        return {
            "source": "lma",
            "id": ident,
            "artist": _first(d.get("creator")),
            "date": _date10(d.get("date")),
            "venue": _first(d.get("venue")),
            "location": _first(d.get("coverage")),
            "source_type": parse_source_type(src_text, ident),
            "format": parse_format(d.get("format")),
            "size_bytes": _to_int(d.get("item_size")),
            "added": _first(d.get("addeddate")),
            "stream_only": "stream_only" in colls,
        }

    # -- one item ------------------------------------------------------------
    def _metadata(self, item_id):
        def load():
            url = "https://archive.org/metadata/" + urllib.parse.quote(item_id, safe="")
            data = self._get_json(url)
            if not data or not data.get("metadata"):
                raise SourceNotFound("Not found")
            return data
        return self._cached(("meta", item_id), _ITEM_TTL, load)

    def _is_stream_only(self, meta, files):
        colls = [str(c).lower() for c in _as_list(meta.get("collection"))]
        if "stream_only" in colls or _truthy(meta.get("access-restricted-item")):
            return True
        # Every audio original private: nothing is downloadable.
        audio = [f for f in (files or [])
                 if str(f.get("source", "")).lower() == "original"
                 and _is_audio(f.get("name") or "")]
        return bool(audio) and all(_truthy(f.get("private")) for f in audio)

    def item(self, item_id):
        data = self._metadata(item_id)
        meta = data.get("metadata") or {}
        files = data.get("files") or []
        originals = _public_originals(files)
        audio = [f for f in originals if _is_audio(f.get("name", ""))]

        fmt_names = [f.get("format", "") for f in files
                     if str(f.get("source", "")).lower() == "original"]
        fmt = parse_format(fmt_names)
        src_text = _first(meta.get("source"))
        audio_bytes = sum(_to_int(f.get("size")) for f in audio)

        # One set of tracks: prefer the lossless copy when an item carries
        # several encodings of the same show.
        track_files = audio
        for ext in (".flac", ".shn"):
            sub = [f for f in audio if f["name"].lower().endswith(ext)]
            if sub:
                track_files = sub
                break
        tracks = []
        for i, f in enumerate(sorted(track_files, key=lambda f: f["name"]), 1):
            n = _to_int(f.get("track"), i)
            tracks.append({"n": n, "title": _first(f.get("title")) or f["name"],
                           "length_s": _length_s(f.get("length"))})

        info_text = ""
        txt = next((f for f in originals if f["name"].lower().endswith(".txt")), None)
        if txt:
            url = self._file_url(item_id, txt["name"])
            try:
                info_text = self._cached(("txt", url), _ITEM_TTL,
                                         lambda: self._get_text(url))
            except SourceError:
                info_text = ""

        return {
            "source": "lma",
            "id": item_id,
            # What a download will actually fetch, for the drawer's Files tab.
            "files": [{"name": f["name"], "size_bytes": _to_int(f.get("size")),
                       "format": f.get("format") or ""}
                      for f in sorted(originals, key=lambda f: f["name"])],
            "artist": _first(meta.get("creator")),
            "date": _date10(meta.get("date")),
            "venue": _first(meta.get("venue")),
            "location": _first(meta.get("coverage")),
            "source_type": parse_source_type(src_text, item_id),
            "format": fmt,
            "size_bytes": _to_int(meta.get("item_size")) or sum(_to_int(f.get("size")) for f in originals),
            "added": _first(meta.get("addeddate")),
            "stream_only": self._is_stream_only(meta, files),
            "source_text": src_text,
            "lineage": _first(meta.get("lineage")),
            "taper": _first(meta.get("taper")),
            "transferer": _first(meta.get("transferer")),
            "tracks": tracks,
            "info_text": info_text,
            "audio_bytes": audio_bytes,
            "url": "https://archive.org/details/" + urllib.parse.quote(item_id, safe=""),
        }

    @staticmethod
    def _file_url(item_id, name):
        return ("https://archive.org/download/" + urllib.parse.quote(item_id, safe="")
                + "/" + urllib.parse.quote(name, safe="/"))

    def download_plan(self, item_id):
        data = self._metadata(item_id)
        return [{"name": f["name"],
                 "url": self._file_url(item_id, f["name"]),
                 "size": _to_int(f.get("size")),
                 "md5": (_first(f.get("md5")) or None)}
                for f in _public_originals(data.get("files"))]
