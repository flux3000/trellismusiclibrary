"""
app/utils/resolver_corpus/lma.py -- the G2 collector (Live Music Archive).

Uses the Internet Archive's official APIs, as app/sources/lma.py does: the search
(scrape) API lists identifiers in collection:etree, /metadata/<id> gives one item.
The sample is the N identifiers with the smallest SHA-1. The key is the item's own
metadata (creator, date, venue, coverage, source), which is uploader-typed and noisy;
the harness reports G2 separately and says so. Local use only, never shipped.

Run: python3 -m app.utils.resolver_corpus g2 [--limit N] [--resume] [--plan]
"""
import json
import math
import re
import urllib.parse

from app.sources import lma as src

from . import common
from .polite import BotProtection, FetchError, RequestCap

SCRAPE_QUERY = "collection:etree AND mediatype:etree"
SCRAPE_COUNT = 10000
ASSUMED_TOTAL = 300_000                 # only for --plan before the first page is cached
EST_REQUESTS_PER_ITEM = 1.9             # one metadata call, about 0.9 text files
SAMPLE_RULE = "the N identifiers with the smallest SHA-1 (no seed, no hand picking)"
MAX_PAGES = 200


def scrape_url(cursor=None):
    params = [("q", SCRAPE_QUERY), ("fields", "identifier"), ("count", str(SCRAPE_COUNT))]
    if cursor:
        params.append(("cursor", cursor))
    return "https://archive.org/services/search/v1/scrape?" + urllib.parse.urlencode(params)


def metadata_url(identifier):
    return "https://archive.org/metadata/" + urllib.parse.quote(identifier, safe="")


def _json(res):
    try:
        return json.loads(res.body.decode("utf-8", "replace"))
    except ValueError:
        return None


def enumerate_ids(fetcher, log=print):
    """Every identifier in the collection, page by page (each page cached)."""
    ids, cursor = [], None
    for page in range(1, MAX_PAGES + 1):
        res = fetcher.get(scrape_url(cursor), max_bytes=32 * 1024 * 1024)
        data = _json(res) if res.ok else None
        if not data or data.get("error") or "items" not in data:
            raise FetchError(f"search API page {page} unusable: "
                             f"{(data or {}).get('error', res.status)}")
        ids += [i["identifier"] for i in data["items"] if i.get("identifier")]
        log(f"  search page {page}: {len(ids)} of {data.get('total', '?')} identifiers")
        cursor = data.get("cursor")
        if not cursor:
            return ids
    raise FetchError("search API did not finish within the page cap")


def _first(v):
    return src._first(v)


def _date_parts(v):
    m = re.match(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", _first(v))
    if not m:
        return None
    return {"y": int(m.group(1)), "m": int(m.group(2)) if m.group(2) and m.group(2) != "00" else None,
            "d": int(m.group(3)) if m.group(3) and m.group(3) != "00" else None}


def _audio_files(files):
    audio = [f for f in files if src._is_audio(f.get("name") or "")]
    for ext in (".flac", ".shn"):
        sub = [f for f in audio if f["name"].lower().endswith(ext)]
        if sub:
            return sub
    return audio


def info_candidates(files):
    return [{"name": f["name"], "size": src._to_int(f.get("size"), None) if f.get("size") else None}
            for f in src._public_originals(files) if f.get("name", "").lower().endswith(".txt")]


def build_item(identifier, data, info_text, info_name):
    """The corpus line for one item. Pure: no network."""
    meta = data.get("metadata") or {}
    files = data.get("files") or []
    audio = sorted(_audio_files(src._public_originals(files)), key=lambda f: f["name"])
    lengths = [src._length_s(f.get("length")) for f in audio]
    src_text = _first(meta.get("source"))
    return {
        "id": identifier,
        "info_text": info_text,
        "folder_name": identifier,
        "n_audio": len(audio),
        "durations": lengths if audio and all(x is not None for x in lengths) else None,
        "archive_meta": {
            "tier": "G2", "noisy": True,
            "creator": _first(meta.get("creator")), "date": _first(meta.get("date")),
            "venue": _first(meta.get("venue")), "coverage": _first(meta.get("coverage")),
            "source": src_text, "lineage": _first(meta.get("lineage")),
            "taper": _first(meta.get("taper")), "transferer": _first(meta.get("transferer")),
            "collection": [str(c) for c in src._as_list(meta.get("collection"))],
            "stream_only": src.LmaSource._is_stream_only(None, meta, files),
            "info_file": info_name,
        },
        "gold": {
            "artist": _first(meta.get("creator")) or None,
            "date": _date_parts(meta.get("date")),
            "venue": _first(meta.get("venue")) or None,
            "coverage_raw": _first(meta.get("coverage")) or None,
            "source": src.parse_source_type(src_text, identifier),
        },
    }


def process_item(fetcher, identifier):
    res = fetcher.get(metadata_url(identifier), max_bytes=16 * 1024 * 1024)
    data = _json(res) if res.ok else None
    if not data or not data.get("metadata"):
        return None, "no_metadata"
    if _first(data["metadata"].get("mediatype")) not in ("", "etree"):
        return None, "not_etree"
    cands = info_candidates(data.get("files") or [])
    if not cands:
        return None, "no_txt"

    def fetch_text(e):
        r = fetcher.get(src.LmaSource._file_url(identifier, e["name"]), max_bytes=common.INFO_MAX_BYTES)
        return common.decode_text(r.body) if r.ok else None

    entry, text = common.pick_info_text(cands, fetch_text)
    if entry is None:
        return None, "no_info_text"
    return build_item(identifier, data, text, entry["name"]), None


def plan(fetcher, sample_n=common.SAMPLE_SIZE, limit=None):
    n = min(sample_n, limit) if limit else sample_n
    first = scrape_url()
    rows = []
    if fetcher.is_cached(first):
        d = _json(fetcher.get(first)) or {}
        total = int(d.get("total") or ASSUMED_TOTAL)
        pages, cursor, have = math.ceil(total / SCRAPE_COUNT), d.get("cursor"), 1
        while cursor and fetcher.is_cached(scrape_url(cursor)):
            have += 1
            cursor = (_json(fetcher.get(scrape_url(cursor))) or {}).get("cursor")
        reqs = max(0, pages - have)
        rows.append(("items in the collection", str(total)))
    else:
        reqs = math.ceil(ASSUMED_TOTAL / SCRAPE_COUNT)
        rows.append(("items in the collection", f"unknown (assuming {ASSUMED_TOTAL})"))
    rows.append(("search pages still needed", str(reqs)))
    rows.append(("items to collect", str(n)))
    reqs += int(round(n * EST_REQUESTS_PER_ITEM))
    return common.plan_text("G2 plan (nothing fetched)", rows, reqs,
                            "estimate: 1 metadata call + about 0.9 text files per item")


def collect(fetcher, out_dir, sample_n=common.SAMPLE_SIZE, limit=None, log=print):
    log("enumerating collection:etree identifiers (cached pages cost nothing)")
    ids = enumerate_ids(fetcher, log)
    sample = common.stable_sample(ids, sample_n)
    if limit:
        sample = sample[:limit]
    log(f"collection: {len(ids)} items; collecting {len(sample)}")
    items, skipped, errors = [], {}, 0
    prog = common.Progress("g2 items", len(sample))
    consecutive, stopped, fatal = 0, None, None
    for ident in sample:
        try:
            item, why = process_item(fetcher, ident)
            consecutive = 0
        except RequestCap as e:
            stopped = str(e)
            break
        except BotProtection as e:
            stopped, fatal = str(e), e
            break
        except FetchError as e:
            item, why, errors = None, "fetch_error", errors + 1
            consecutive += 1
            log(f"! {e}")
            if consecutive >= 5:
                stopped = "5 fetch errors in a row"
                break
        if item:
            items.append(item)
        else:
            skipped[why] = skipped.get(why, 0) + 1
        prog.tick(fetcher, f"kept {len(items)}")
    meta = {
        "source": "Internet Archive, collection:etree (search/scrape API + /metadata)",
        "sample_rule": SAMPLE_RULE, "sample_size": len(sample), "collection_items": len(ids),
        "kept": len(items), "skipped": skipped, "fetch_errors": errors,
        "stream_only_kept": sum(1 for i in items if i["archive_meta"]["stream_only"]),
        "requests": fetcher.requests, "cache_hits": fetcher.cache_hits,
        "answer_key": "item metadata; NOISY (uploader-typed); artist and date compared exactly, "
                      "venue and coverage loosely",
        "note": "items without a usable info text are not written (counted under skipped)",
        "complete": stopped is None, "stopped": stopped,
    }
    path = common.write_corpus(out_dir, "G2", items, meta)
    log(f"wrote {path} ({len(items)} items); skipped {skipped}"
        + (f"; STOPPED EARLY: {stopped}" if stopped else ""))
    if fatal:
        raise fatal
    return meta
