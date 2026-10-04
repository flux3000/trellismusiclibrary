"""
Wikidata for the Atlas builder, in three small passes that cannot hit the 60 s query limit
(Resolver v2, chunk 4). Wikidata data is CC0.

  Pass 1  ids.     SPARQL, ids only: no label service, no name or alias joins, no deep
                   traversal in the same query. The subclass list of each class is its own
                   small query; then each class's direct P31 members are paged with
                   ORDER BY and LIMIT/OFFSET, at most 5,000 ids a page.
  Pass 2  details. The Action API, wbgetentities, 50 ids a call: labels and aliases (English
                   only), claims P1448 / P2561 (names, with start and end years), P571, P576,
                   P625, P131, P17.
  Pass 3  places.  The English labels of the P131 places and the country codes of the P17
                   countries named in pass 2, same API.

Everything is cached under <cache>/wikidata/, so a rerun resumes rather than restarts.
Every request carries the project's User-Agent, waits at least a second after it, sends
maxlag, and honours Retry-After. A request that fails in any transient way (dropped
connection, truncated body, timeout, bad JSON, 429/5xx) is retried; a source that still
fails raises WikidataError, which the caller reports instead of crashing the build.
"""
import http.client
import json
import math
import re
import socket
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import build as B
from . import schema as S

SPARQL = "https://query.wikidata.org/sparql"
API = "https://www.wikidata.org/w/api.php"
LANGS = ("en", "mul")
IDS_PAGE = 5000
MIN_PAGE = 500
BATCH = 50
MIN_API_DELAY = 1.0
SUBCLASS_CHUNK = 150
PER_CALL_SECONDS = 0.6          # typical response time, for the estimate

# qid -> (acceptable English labels, kind). Each is verified at run time; a class whose
# label is not what is expected here is skipped and reported, never used on trust.
WD_CLASSES = {
    "Q1060829": (("concert hall",), "venue"),
    "Q8719053": (("music venue",), "venue"),
    "Q24354":   (("theatre", "theater"), "venue"),
    "Q483110":  (("stadium",), "venue"),
    "Q641226":  (("arena",), "venue"),
    "Q622425":  (("nightclub", "night club"), "venue"),
    "Q868557":  (("music festival",), "festival"),
}
MAX_NAMES = 40


class WikidataError(B.BuildError):
    pass


_sleep = time.sleep             # tests replace this


# ── requests ─────────────────────────────────────────────────────────────────

RETRY_HTTP = {408, 425, 429, 500, 502, 503, 504}


def is_retryable(exc):
    """A failure worth another try: the connection, the body, the clock or the status."""
    if isinstance(exc, B.BuildError):
        return False
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRY_HTTP
    return isinstance(exc, (urllib.error.URLError, http.client.HTTPException, ConnectionError,
                            socket.timeout, TimeoutError, ValueError, OSError, _Lag))


class _Lag(Exception):
    """The API asked clients to back off (maxlag)."""

    def __init__(self, wait):
        super().__init__(f"maxlag, wait {wait}s")
        self.wait = wait


def _wait_for(exc, i):
    h = getattr(exc, "headers", None)
    v = h.get("Retry-After") if h else None
    if v and str(v).isdigit():
        return min(300, int(v))
    if isinstance(exc, _Lag):
        return min(300, max(1, exc.wait))
    return min(120, 5 * 2 ** i)


def get_json(url, accept, *, validate=None, tries=6, timeout=90, what="Wikidata request"):
    """GET and decode JSON, retrying every transient failure. Raises WikidataError when tries run out."""
    last = None
    for i in range(tries):
        try:
            with B.http_open(url, {"Accept": accept}, timeout=timeout) as r:
                body = r.read()
            data = json.loads(body.decode("utf-8"))
            if validate:
                validate(data)
            return data
        except Exception as e:                      # noqa: BLE001 -- classified below
            if not is_retryable(e):
                raise WikidataError(f"{what}: {type(e).__name__}: {e}") from e
            last = e
            if i + 1 < tries:
                _sleep(_wait_for(e, i))
    raise WikidataError(f"{what}: no usable answer after {tries} tries ({type(last).__name__}: {last})")


def sparql_rows(query, delay, what):
    def ok(d):
        if "results" not in d or "bindings" not in d["results"]:
            raise ValueError("no results in the answer")
    url = SPARQL + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
    data = get_json(url, "application/sparql-results+json", validate=ok, timeout=75, what=what)
    _sleep(delay)
    return data["results"]["bindings"]


def get_entities(ids, props, api_delay, what):
    """wbgetentities for up to 50 ids -> {qid: entity} (missing entities left out)."""
    def ok(d):
        err = d.get("error")
        if err and err.get("code") == "maxlag":
            raise _Lag(int(err.get("lag") or 5))
        if err:
            raise B.BuildError(f"the API refused the request: {err.get('code')}: {err.get('info')}")
        if not isinstance(d.get("entities"), dict):
            raise ValueError("no entities in the answer")
    url = API + "?" + urllib.parse.urlencode({
        "action": "wbgetentities", "ids": "|".join(ids), "props": props, "languages": "|".join(LANGS),
        "format": "json", "maxlag": "5"})
    data = get_json(url, "application/json", validate=ok, what=what)
    _sleep(max(MIN_API_DELAY, api_delay))
    return {q: e for q, e in data["entities"].items() if not e.get("missing")}


# ── pass 1: ids ──────────────────────────────────────────────────────────────

def _qid(uri):
    m = re.search(r"/(Q\d+)$", uri or "")
    return m.group(1) if m else None


def _val(row, key):
    d = row.get(key)
    return d.get("value") if d else None


def verify_classes(delay, log):
    """Keep only the classes whose English label is what WD_CLASSES expects."""
    vals = " ".join("wd:" + q for q in WD_CLASSES)
    q = "SELECT ?c ?l WHERE { VALUES ?c { " + vals + ' } ?c rdfs:label ?l FILTER(LANG(?l) = "en") }'
    try:
        rows = sparql_rows(q, delay, "class check")
    except WikidataError as e:
        log(f"Wikidata class check failed ({e}); using the classes unchecked")
        return dict(WD_CLASSES)
    got = {_qid(_val(r, "c")): (_val(r, "l") or "").lower() for r in rows}
    ok = {}
    for qid, (labels, kind) in WD_CLASSES.items():
        if got.get(qid) and any(w in got[qid] for w in labels):
            ok[qid] = (labels, kind)
        else:
            log(f"Wikidata class {qid} is labelled {got.get(qid)!r}, expected one of {labels}: skipped")
    return ok


def subclass_ids(qid, delay):
    rows = sparql_rows(f"SELECT ?c WHERE {{ ?c wdt:P279* wd:{qid} . }}", delay, f"subclasses of {qid}")
    return sorted({_qid(_val(r, "c")) for r in rows} - {None} | {qid})


def members_query(classes, limit, offset):
    vals = " ".join("wd:" + c for c in classes)
    return (f"SELECT DISTINCT ?item WHERE {{ VALUES ?c {{ {vals} }} ?item wdt:P31 ?c . }} "
            f"ORDER BY ?item LIMIT {limit} OFFSET {offset}")


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def fetch_ids(cache, classes, delay, sample, log):
    """Pass 1 -> ({qid: kind}, [failed class qids], n_subclasses). Resumes from the cache."""
    ids, failed, n_sub = {}, [], 0
    for root, (labels, kind) in classes.items():
        path = cache / f"ids-{root}.json" if cache else None
        st = _load(path) if path else None
        if not st:
            try:
                subs = subclass_ids(root, delay)
            except WikidataError as e:
                failed.append(root)
                log(f"Wikidata {labels[0]}: subclass list failed ({e}); class skipped")
                continue
            st = {"subs": subs, "ids": [], "chunk": 0, "offset": 0, "done": False}
        n_sub += len(st["subs"])
        chunks = [st["subs"][i:i + SUBCLASS_CHUNK] for i in range(0, len(st["subs"]), SUBCLASS_CHUNK)]
        size = IDS_PAGE if not sample else min(IDS_PAGE, sample)
        while not st["done"] and st["chunk"] < len(chunks):
            try:
                rows = sparql_rows(members_query(chunks[st["chunk"]], size, st["offset"]), delay,
                                   f"{labels[0]} members at {st['offset']}")
            except WikidataError as e:
                if size > MIN_PAGE:
                    size = max(MIN_PAGE, size // 2)
                    log(f"Wikidata {labels[0]}: page failed ({e}); retrying with {size} ids")
                    continue
                failed.append(root)
                log(f"Wikidata {labels[0]} given up at offset {st['offset']}: {e}")
                break
            got = [q for q in (_qid(_val(r, "item")) for r in rows) if q]
            st["ids"].extend(got)
            if len(rows) < size or sample:
                st["chunk"] += 1
                st["offset"] = 0
            else:
                st["offset"] += size
            if sample and st["chunk"] >= 1:
                st["done"] = True
            if path and not sample:
                _save(path, st)
            log(f"Wikidata {labels[0]}: {len(st['ids']):,} ids")
        else:
            st["done"] = True
            if path and not sample:
                _save(path, st)
        for q in st["ids"]:
            ids.setdefault(q, kind)
    return ids, failed, n_sub


# ── pass 2: entity details ───────────────────────────────────────────────────

def _claims(ent, pid):
    cs = [c for c in (ent.get("claims") or {}).get(pid, [])
          if c.get("rank") != "deprecated" and (c.get("mainsnak") or {}).get("snaktype") == "value"]
    return sorted(cs, key=lambda c: c.get("rank") != "preferred")


def _snak_value(snak):
    return ((snak or {}).get("datavalue") or {}).get("value")


def _year(time_value):
    m = re.match(r"^\+?(\d{4})", str((time_value or {}).get("time") if isinstance(time_value, dict) else time_value or ""))
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def _qual_year(claim, pid):
    for sn in (claim.get("qualifiers") or {}).get(pid, []):
        y = _year(_snak_value(sn))
        if y:
            return y
    return None


def _entity_ref(claim):
    v = _snak_value(claim.get("mainsnak"))
    return v.get("id") if isinstance(v, dict) else None


def extract_entity(ent):
    """One wbgetentities entity -> {"item": {...} | None, "names": [...]}. The item has no
    kind yet (pass 1 knows it) and carries _loc / _ctry, the ids pass 3 turns into text."""
    q = ent.get("id")
    labels, aliases = ent.get("labels") or {}, ent.get("aliases") or {}
    label = next((labels[l]["value"] for l in LANGS if l in labels), None)
    if not q or not label:
        return {"q": q, "item": None, "names": []}
    item = {"qid": q, "label": label.strip(), "lat": None, "lon": None, "loc": None, "cc": None,
            "inception": None, "dissolved": None, "_loc": None, "_ctry": None}
    for c in _claims(ent, "P625")[:1]:
        v = _snak_value(c["mainsnak"]) or {}
        if isinstance(v.get("latitude"), (int, float)) and isinstance(v.get("longitude"), (int, float)):
            item["lat"], item["lon"] = float(v["latitude"]), float(v["longitude"])
    locs = [c for c in _claims(ent, "P131") if not (c.get("qualifiers") or {}).get("P582")] or _claims(ent, "P131")
    item["_loc"] = next((r for r in map(_entity_ref, locs) if r), None)
    item["_ctry"] = next((r for r in map(_entity_ref, _claims(ent, "P17")) if r), None)
    for pid, key in (("P571", "inception"), ("P576", "dissolved")):
        for c in _claims(ent, pid):
            y = _year(_snak_value(c["mainsnak"]))
            if y:
                item[key] = y
                break
    names, seen = [], set()
    for pid in ("P1448", "P2561"):
        for c in _claims(ent, pid):
            v = _snak_value(c["mainsnak"])
            text, lang = (v.get("text"), v.get("language")) if isinstance(v, dict) else (v, "en")
            if not text or (lang or "en").split("-")[0] not in LANGS:
                continue
            s, e = _qual_year(c, "P580"), _qual_year(c, "P582")
            kind = "former" if e else ("official" if s else "alias")
            if kind == "official":
                kind = "alias"          # an undated official name says nothing about when
            names.append([text.strip(), kind, s, e])
    for l in LANGS:
        for a in aliases.get(l, []):
            names.append([a["value"].strip(), "alias", None, None])
    out = []
    for n, kind, s, e in names:
        k = (S.norm_key(n), s, e)
        if S.norm_key(n) and k not in seen and S.norm_key(n) != S.norm_key(label):
            seen.add(k)
            out.append([n, kind, s, e])
    return {"q": q, "item": item, "names": out[:MAX_NAMES]}


def fetch_details(cache, ids, api_delay, log, estimate=None):
    """Pass 2 -> ({qid: record}, failed_batches). Appends to <cache>/entities.jsonl as it goes."""
    path = cache / "entities.jsonl" if cache else None
    have = {}
    if path and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                have[r["q"]] = r
            except (ValueError, KeyError):
                continue
    todo = sorted(q for q in ids if q not in have)
    if have:
        log(f"Wikidata details: {len(have):,} cached, {len(todo):,} to fetch")
    failed = 0
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    fh = open(path, "a", encoding="utf-8") if path and batches else None
    try:
        for n, batch in enumerate(batches, 1):
            try:
                ents = get_entities(batch, "labels|aliases|claims", api_delay, f"entities {batch[0]}..{batch[-1]}")
            except WikidataError as e:
                failed += 1
                log(f"Wikidata details: batch {batch[0]}..{batch[-1]} skipped ({e})")
                continue
            for q in batch:
                rec = extract_entity(ents[q]) if q in ents else {"q": q, "item": None, "names": []}
                rec["q"] = q
                have[q] = rec
                if fh:
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            if fh:
                fh.flush()
            if n % 20 == 0 or n == len(batches):
                log(f"Wikidata details: {n:,} of {len(batches):,} calls")
    finally:
        if fh:
            fh.close()
    return {q: have[q] for q in ids if q in have}, failed


# ── pass 3: place labels and country codes ───────────────────────────────────

def fetch_refs(cache, records, api_delay, log):
    """-> {"labels": {qid: text}, "countries": {qid: iso}}; resumes from <cache>/refs.json."""
    path = cache / "refs.json" if cache else None
    refs = (_load(path) if path else None) or {"labels": {}, "countries": {}}
    locs = sorted({r["item"]["_loc"] for r in records.values() if r["item"] and r["item"]["_loc"]} - set(refs["labels"]))
    ctrs = sorted({r["item"]["_ctry"] for r in records.values() if r["item"] and r["item"]["_ctry"]} - set(refs["countries"]))
    calls = math.ceil(len(locs) / BATCH) + math.ceil(len(ctrs) / BATCH)
    if calls:
        log(f"Wikidata places: {len(locs):,} place labels and {len(ctrs):,} countries to look up, "
            f"{calls:,} calls, about {calls * (max(MIN_API_DELAY, api_delay) + PER_CALL_SECONDS) / 60:.0f} min")
    for ids, props, key in ((locs, "labels", "labels"), (ctrs, "claims", "countries")):
        for i in range(0, len(ids), BATCH):
            batch = ids[i:i + BATCH]
            try:
                ents = get_entities(batch, props, api_delay, f"{key} {batch[0]}..{batch[-1]}")
            except WikidataError as e:
                log(f"Wikidata places: batch {batch[0]}..{batch[-1]} skipped ({e})")
                continue
            for q in batch:
                e = ents.get(q) or {}
                if key == "labels":
                    lab = (e.get("labels") or {})
                    refs["labels"][q] = next((lab[l]["value"] for l in LANGS if l in lab), "")
                else:
                    cc = next((v for v in (_snak_value(c["mainsnak"]) for c in _claims(e, "P297"))
                               if isinstance(v, str)), "")
                    refs["countries"][q] = cc
            if path:
                _save(path, refs)
    return refs


# ── the whole fetch ──────────────────────────────────────────────────────────

def estimate_text(n_classes, n_ids, api_delay):
    calls = math.ceil(n_ids / BATCH)
    extra = math.ceil(calls * 0.4)
    mins = (calls + extra) * (max(MIN_API_DELAY, api_delay) + PER_CALL_SECONDS) / 60
    return (f"Wikidata estimate: {n_classes} classes, {n_ids:,} ids; pass 2 needs {calls:,} calls "
            f"(plus about {extra:,} for place labels), about {mins:.0f} min at "
            f"{max(MIN_API_DELAY, api_delay):.0f} s a call")


def cached_estimate(cache, api_delay):
    """For --plan: the estimate from a cached pass 1, or None."""
    ids = set()
    n = 0
    for p in sorted(Path(cache).glob("wikidata/ids-Q*.json")):
        st = _load(p)
        if st:
            n += 1
            ids.update(st.get("ids", []))
    return estimate_text(n, len(ids), api_delay) if n else None


def fetch(cache_dir, *, delay=2.0, api_delay=1.0, sample=None, refresh=False, log=B.say):
    """-> (items {qid: item}, names [(qid, name, kind, start, end)], info). Raises WikidataError when
    nothing usable came back; classes or batches that failed are listed in info["failed"]."""
    cache = None if sample else Path(cache_dir) / "wikidata"
    if cache and refresh and cache.exists():
        for p in cache.glob("*"):
            p.unlink()
    log("Wikidata: pass 1 of 3, ids only (SPARQL)")
    classes = verify_classes(delay, log)
    if not classes:
        raise WikidataError("no Wikidata class passed its label check")
    log(f"Wikidata: {len(classes)} classes: " + ", ".join(f"{q} {v[0][0]}" for q, v in classes.items()))
    kinds, failed, n_sub = fetch_ids(cache, classes, delay, sample, log)
    if not kinds:
        raise WikidataError("pass 1 returned no ids" + (f" (failed classes: {', '.join(failed)})" if failed else ""))
    log(estimate_text(len(classes), len(kinds), api_delay))
    log("Wikidata: pass 2 of 3, names, aliases and claims (Action API)")
    records, bad = fetch_details(cache, list(kinds), api_delay, log)
    log("Wikidata: pass 3 of 3, place labels and countries")
    refs = fetch_refs(cache, records, api_delay, log)
    items, names = {}, []
    for q in sorted(records, key=lambda x: int(x[1:])):
        rec = records[q]
        it = rec.get("item")
        if not it:
            continue
        it = dict(it, kind=kinds[q])
        it["loc"] = refs["labels"].get(it.pop("_loc")) or None
        it["cc"] = refs["countries"].get(it.pop("_ctry")) or None
        items[q] = it
        names.extend((q, n, k, s, e) for n, k, s, e in rec["names"])
    if not items:
        raise WikidataError("no item came back from the entity calls")
    info = {"classes": sorted(classes), "subclasses": n_sub, "ids": len(kinds), "items": len(items),
            "failed": failed, "failed_batches": bad, "method": "SPARQL ids + wbgetentities",
            "retrieved": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    return items, names, info
