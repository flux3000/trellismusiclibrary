"""
app/utils/commons.py — freely-licensed performer photos via Wikidata → Commons.

MusicBrainz hosts no artist images. What it does give us is a WIKIDATA link
(captured in `mb_links_json`), and Wikidata's property P18 points at a file on
Wikimedia Commons. That chain is the whole feature:

    MusicBrainz  →  Wikidata QID  →  P18  →  Commons file  →  bytes + licence

WHY COMMONS, AND ONLY COMMONS
-----------------------------
Commons accepts freely-licensed files ONLY — public domain, CC0, CC BY,
CC BY-SA. Non-commercial and no-derivatives licences are rejected at upload, so
anything here is safe to redistribute, which matters because peer sharing will
eventually expose this library beyond one Mac.

The trap this avoids: **English Wikipedia hosts non-free "fair use" images
locally**, and they look identical in a page scrape. Fetching from the Commons
API rather than from article HTML is precisely what keeps the result clean. We
never touch Wikipedia's own file namespace.

Attribution is captured, not assumed: CC BY and CC BY-SA both require credit,
so the licence string and author land in `PerformerImage.credit` at fetch time.
A photo whose licence we cannot read is REJECTED rather than stored with a
guess — an unattributable image is worse than no image.

Everything fails soft, like musicbrainz.py: offline is a supported state and a
missing photo must never break a page or an ingest.
"""

import io
import re
import json
import time
import logging
import urllib.parse
import urllib.request
import urllib.error

from app.utils.net import SSL_CONTEXT

log = logging.getLogger(__name__)

_UA = "FluxAudio/1.0 ( https://github.com/flux3000/fluxaudio )"
_TIMEOUT = 12.0          # higher than MusicBrainz: this downloads image bytes
_MIN_INTERVAL = 0.4      # Wikimedia is more permissive than MB, but be polite
_last_call = [0.0]

# Refuse anything that isn't clearly free. Commons shouldn't contain NC/ND at
# all, so this is a belt-and-braces check on the machine-readable licence code
# rather than an expected code path — but "shouldn't" is not a licence audit.
_BLOCKED_LICENCE_BITS = ("-nc", "-nd", "noncommercial", "nonderiv", "fairuse", "non-free")

_ALLOWED_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

# Commons thumbnails rather than originals: source files are routinely
# 20-40 MB scans. 900px is ample for a 104px hero portrait and a gallery tile.
_THUMB_WIDTH = 900
_MAX_BYTES = 12 * 1024 * 1024


def _throttle():
    gap = time.time() - _last_call[0]
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    _last_call[0] = time.time()


def _get_json(url):
    """
    GET and parse one MediaWiki API response, or None.

    IMPORTANT: MediaWiki returns HTTP 200 for parameter errors, with the problem
    in an `error` key. Treating that as data is how a broken request became
    "this artist has no photo" for every artist at once (2026-08-08 — the
    wbgetclaims `property` bug below). Errors are now surfaced at WARNING, not
    swallowed: a systematic failure has to look different from an ordinary miss.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        _throttle()
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=SSL_CONTEXT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ValueError, OSError) as e:
        log.info("commons/wikidata fetch failed (%s): %s", url, e)
        return None

    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        log.warning("commons/wikidata API error (%s): %s — %s", url,
                    err.get("code"), err.get("info"))
        return None
    return data


def qid_from_links(links):
    """
    Pull a Wikidata QID out of the MusicBrainz link map.

    `links` is the dict stored in `mb_links_json` — {'Wikidata': 'https://...'}.
    Returns 'Q12345' or None. Tolerates the URL shapes Wikidata uses
    (www.wikidata.org/wiki/Q…, /entity/Q…).
    """
    for key, url in (links or {}).items():
        if "wikidata.org" in (url or ""):
            m = re.search(r"/(Q\d+)", url)
            if m:
                return m.group(1)
    return None


def image_filenames_for_qid(qid):
    """
    Every image Wikidata offers for an entity, best first.

    P18 claims come first — that's the designated main image, and entities can
    carry several. The Commons CATEGORY (P373) is then walked as a fallback so a
    second click has somewhere to go: P18 alone is usually a single photo, and
    "get me another one" has to come from somewhere.

    Categories are messier than P18 (they hold album covers, venue shots,
    posters), which is exactly why they rank below it rather than replacing it.
    """
    if not qid:
        return []
    # wbgetentities, NOT wbgetclaims (fixed 2026-08-08). wbgetclaims' `property`
    # parameter takes a SINGLE property id; the pipe-joined "P18|P373" this used
    # to send failed parameter validation, and MediaWiki reports that as HTTP 200
    # with an `error` body and no `claims` key — which read as "no images" for
    # every artist in the library. wbgetentities accepts many properties at once
    # and is the right call for wanting two of them.
    url = ("https://www.wikidata.org/w/api.php?action=wbgetentities"
           f"&ids={urllib.parse.quote(qid)}&props=claims&format=json")
    data = _get_json(url)
    if not data:
        return []
    entities = data.get("entities") or {}
    entity = entities.get(qid) or (next(iter(entities.values()), {}) if entities else {})
    claims = entity.get("claims") or {}

    names = []
    for claim in claims.get("P18", []):
        try:
            names.append(claim["mainsnak"]["datavalue"]["value"])
        except (KeyError, TypeError):
            continue

    for claim in claims.get("P373", []):
        try:
            category = claim["mainsnak"]["datavalue"]["value"]
        except (KeyError, TypeError):
            continue
        names.extend(_category_files(category))
        break                       # one category is plenty

    # Preserve order, drop repeats (a P18 image is normally in the category too)
    out, seen = [], set()
    for n in names:
        if n and n not in seen:
            out.append(n)
            seen.add(n)
    return out


def _category_files(category):
    """Image filenames in a Commons category, newest-listed first."""
    title = category if category.lower().startswith("category:") else f"Category:{category}"
    url = ("https://commons.wikimedia.org/w/api.php?action=query&format=json"
           f"&list=categorymembers&cmtitle={urllib.parse.quote(title)}"
           "&cmtype=file&cmlimit=25")
    data = _get_json(url)
    if not data:
        return []
    members = (data.get("query") or {}).get("categorymembers") or []
    out = []
    for m in members:
        t = (m.get("title") or "")
        if t.lower().startswith("file:"):
            t = t[5:]
        if t.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            out.append(t)
    return out


def _strip_html(s):
    """Commons returns author/licence fields as HTML fragments with links."""
    if not s:
        return None
    txt = re.sub(r"<[^>]+>", "", str(s))
    txt = (txt.replace("&amp;", "&").replace("&nbsp;", " ")
              .replace("&quot;", '"').replace("&#039;", "'")
              .replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", txt).strip() or None


def file_info(filename):
    """
    URL + licence metadata for one Commons file.

    Returns a dict or None. `None` also covers "the licence is unreadable or
    not free" — the caller cannot store an image it can't attribute, so the
    distinction between missing and rejected is only a log line.
    """
    if not filename:
        return None
    title = filename if filename.lower().startswith("file:") else f"File:{filename}"
    url = ("https://commons.wikimedia.org/w/api.php?action=query&format=json"
           f"&titles={urllib.parse.quote(title)}"
           "&prop=imageinfo&iiprop=url|extmetadata|mime"
           f"&iiurlwidth={_THUMB_WIDTH}")
    data = _get_json(url)
    if not data:
        return None
    try:
        page = next(iter(data["query"]["pages"].values()))
        info = page["imageinfo"][0]
    except (KeyError, TypeError, StopIteration, IndexError):
        return None

    meta = info.get("extmetadata") or {}
    code = (meta.get("License", {}).get("value") or "").lower()
    short = _strip_html(meta.get("LicenseShortName", {}).get("value"))
    author = _strip_html(meta.get("Artist", {}).get("value"))

    if not (code or short):
        log.info("commons: no licence metadata for %s — rejected", filename)
        return None
    if any(bit in code for bit in _BLOCKED_LICENCE_BITS):
        log.info("commons: non-free licence %r for %s — rejected", code, filename)
        return None

    return {
        "filename":  filename,
        # thumburl when the scaler produced one, else the original.
        "url":       info.get("thumburl") or info.get("url"),
        "descurl":   info.get("descriptionurl"),
        "mime":      info.get("mime"),
        "licence":   short or code,
        "author":    author,
        # Ready-to-store attribution line. CC BY and BY-SA both require credit,
        # so this is built once, here, where the metadata actually is.
        "credit":    " · ".join(x for x in (author, short or code,
                                            "via Wikimedia Commons") if x),
    }


def download(url, max_bytes=_MAX_BYTES):
    """
    Fetch image bytes. Returns (bytes, ext) or (None, None).

    Extension comes from the served Content-Type, not the URL: Commons thumbnail
    URLs end in the ORIGINAL extension even when the scaler returns a different
    format, so trusting the path would mislabel files on disk.
    """
    if not url:
        return None, None
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        _throttle()
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=SSL_CONTEXT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            ext = _ALLOWED_MIME.get(ctype)
            if not ext:
                log.info("commons: unsupported content-type %r", ctype)
                return None, None
            buf = io.BytesIO()
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                buf.write(chunk)
                if buf.tell() > max_bytes:
                    log.info("commons: image exceeded %d bytes — abandoned", max_bytes)
                    return None, None
            return buf.getvalue(), ext
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        log.info("commons image download failed (%s): %s", url, e)
        return None, None


def find_photo_for_performer(performer, exclude=None):
    """
    Whole chain for one Performer: links → QID → images → Commons → bytes.

    `exclude` is a set of Commons filenames already imported for this act, so
    clicking "Find a free photo" a second time returns a DIFFERENT photo rather
    than the same one again. Walks candidates in order and returns the first
    that is new, freely licensed, and downloadable.

    Returns {'data', 'ext', 'credit', 'caption', 'source_ref', 'source_url'} or
    None. None is the ordinary outcome, not a failure: most acts have no
    Wikidata entry, and those that do usually have one photo — so the SECOND
    click legitimately comes back empty for almost everything.
    """
    try:
        links = json.loads(performer.mb_links_json) if performer.mb_links_json else {}
    except (TypeError, ValueError):
        links = {}

    qid = qid_from_links(links)
    if not qid:
        return None

    seen = {s for s in (exclude or set()) if s}
    for fname in image_filenames_for_qid(qid):
        if fname in seen:
            continue
        info = file_info(fname)
        if not info:
            continue                 # non-free or unreadable licence — skip on
        data, ext = download(info["url"])
        if not data:
            continue
        return {
            "data":       data,
            "ext":        ext,
            "credit":     info["credit"],
            "caption":    info.get("licence"),
            "source_ref": fname,
            "source_url": info.get("descurl"),
        }
    return None
