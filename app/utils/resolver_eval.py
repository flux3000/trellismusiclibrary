"""
app/utils/resolver_eval.py -- evaluation harness for the ingest resolver.

Resolver v2, chunk 0 (2026-10-03). Every later chunk is gated on a diff from
this module, so it lives in the app, not in tools/: it is a capability.

Run:
    python3 -m app.utils.resolver_eval --tier G1 --reader-only
    python3 -m app.utils.resolver_eval --tier G1 --resolve --diff
    python3 -m app.utils.resolver_eval --export-g1            # snapshot -> g1.jsonl

Tiers
    G1  Ryan's library: snapshot rows joined to the stored info text
        (corpus/g1.jsonl, written by --export-g1)
    G2  Live Music Archive sample           (corpus/g2.jsonl)
    G3  bluegrassarchive.com sample         (corpus/g3.jsonl)
    G4  hand-written failure cases          (tests/fixtures/resolver/)
G1 to G3 live OUTSIDE every repo (default ~/Workshop/dev/resolver-corpus/);
the repo is public. Only G4 is committed.

Item shape (JSONL line, or a G4 .json file with an optional sibling .txt):
    {id, info_text, folder_name, tags?, archive_meta?, n_audio?, durations?,
     gold: {artist, date{y,m,d}, venue, event, stage, city, state, country, source}}
A gold field that is absent or empty is "key has no value" and is not scored;
gold False (fixtures) means the field is expected to stay empty.

Modes
    --reader-only  info text alone, through parse_info_file()
    --resolve      builds a minimal scan payload and calls resolve() + verdict()

No network, no database writes. The snapshot is opened read-only, in place.
"""
import argparse
import json
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

FIELDS = ("artist", "date", "venue", "event", "stage",
          "city", "state", "country", "source")
TIERS = ("G1", "G2", "G3", "G4")
JUDGMENTS = ("right", "partial", "wrong", "empty")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = REPO_ROOT.parent / "resolver-corpus"   # ~/Workshop/dev/resolver-corpus
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "resolver"
DEFAULT_SNAPSHOT = REPO_ROOT / "db" / "_cowork.db"
LAST_RUN = "last_run.json"


# ── Normalising and judging ──────────────────────────────────────────────────
# Same rules as the 2026-10-03 baseline (proposal 2.1): accents, case,
# punctuation, "&"/"and" and a leading "The" are ignored; "partial" means one
# string contains the other.

def norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^the ", "", s)


_US_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "district of columbia": "dc", "florida": "fl", "georgia": "ga", "hawaii": "hi",
    "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri",
    "south carolina": "sc", "south dakota": "sd", "tennessee": "tn", "texas": "tx",
    "utah": "ut", "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "ontario": "on", "quebec": "qc", "british columbia": "bc", "alberta": "ab",
    "manitoba": "mb", "nova scotia": "ns", "new brunswick": "nb",
}
_COUNTRIES = {
    "usa": "us", "u s a": "us", "u s": "us", "united states": "us",
    "united states of america": "us", "america": "us",
    "uk": "uk", "u k": "uk", "united kingdom": "uk", "great britain": "uk",
    "gb": "uk", "england": "uk", "canada": "ca", "france": "fr", "germany": "de",
    "netherlands": "nl", "the netherlands": "nl", "holland": "nl",
    "switzerland": "ch", "australia": "au", "japan": "jp",
}


def _canon_state(s):
    n = norm(s)
    return _US_STATES.get(n, n)


def _canon_country(s):
    n = norm(s)
    return _COUNTRIES.get(n, n)


def _has_words(small, big):
    """True when `small` appears in `big` on word boundaries."""
    return f" {small} " in f" {big} "


def judge_text(gold, pred, canon=norm):
    """right / partial / wrong / empty, or None when the key has no value.

    A gold of False means "expected empty" (hand-written fixtures only): any
    value is wrong, none is right.
    """
    if gold is False:
        return "right" if not canon(pred) else "wrong"
    g = canon(gold)
    if not g:
        return None
    p = canon(pred)
    if not p:
        return "empty"
    if g == p:
        return "right"
    if _has_words(g, p) or _has_words(p, g):
        return "partial"
    return "wrong"


def _date_tuple(d):
    if not d:
        return None
    if isinstance(d, dict):
        d = (d.get("year", d.get("y")), d.get("month", d.get("m")), d.get("day", d.get("d")))
    d = tuple(d) + (None,) * (3 - len(d))
    return tuple(x or None for x in d[:3])


def judge_date(gold, pred):
    """
    Component-wise. right = every component equal. wrong = any component the
    prediction states that the key does not state or states differently
    (an invented month or day lands here). partial = consistent but less
    precise (year only against a full date).
    """
    g, p = _date_tuple(gold), _date_tuple(pred)
    if not g or not g[0]:
        return None
    if not p or not p[0]:
        return "empty"
    if g == p:
        return "right"
    for a, b in zip(g, p):
        if b is not None and a != b:
            return "wrong"
    return "partial"


def judge_field(field, gold, pred):
    if field == "date":
        return judge_date(gold, pred)
    if field == "state":
        return judge_text(gold, pred, _canon_state)
    if field == "country":
        return judge_text(gold, pred, _canon_country)
    return judge_text(gold, pred)


# ── Loading tiers ────────────────────────────────────────────────────────────

def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_fixtures(fixtures_dir=None):
    """G4: *.json (one item, text in a sibling .txt unless inline) and *.jsonl."""
    d = Path(fixtures_dir or FIXTURES_DIR)
    items = []
    for p in sorted(d.glob("*.json")):
        item = json.loads(p.read_text(encoding="utf-8"))
        txt = p.with_suffix(".txt")
        if "info_text" not in item and txt.exists():
            item["info_text"] = txt.read_text(encoding="utf-8")
        item.setdefault("id", p.stem)
        items.append(item)
    for p in sorted(d.glob("*.jsonl")):
        items.extend(_read_jsonl(p))
    return items


def load_tier(tier, corpus_dir=None, fixtures_dir=None):
    """Returns (items, meta). meta carries the snapshot date when the tier has one."""
    if tier == "G4":
        return load_fixtures(fixtures_dir), {"source": "tests/fixtures/resolver"}
    corpus = Path(corpus_dir or DEFAULT_CORPUS)
    path = corpus / f"{tier.lower()}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found (G1: run --export-g1; G2/G3 come in chunk 7)")
    meta = {"source": str(path)}
    meta_path = corpus / f"{tier.lower()}.meta.json"
    if meta_path.exists():
        meta.update(json.loads(meta_path.read_text(encoding="utf-8")))
    return _read_jsonl(path), meta


# ── G1 export ────────────────────────────────────────────────────────────────

_G1_SQL = """
select r.id, r.info_file_content, r.original_folder_name, r.source,
       a.name, p.start_year, p.start_month, p.start_day, p.stage,
       p.city, p.state, p.country,
       v.name, v.city, v.state, v.country, e.name
from recording r
join performance p on p.id = r.performance_id
left join artist a on a.id = p.artist_id
left join venue  v on v.id = p.venue_id
left join event  e on e.id = p.event_id
where r.info_file_content is not null and trim(r.info_file_content) != ''
order by r.id
"""


def _nfc(s):
    # macOS stores filenames decomposed, SQLite composed (CONTEXT.md trap).
    return unicodedata.normalize("NFC", s) if isinstance(s, str) else s


def export_g1(db_path=None, out_dir=None):
    """Snapshot rows -> corpus/g1.jsonl (+ g1.meta.json). Read-only, in place."""
    from app.utils.venues import is_placeholder_venue_name
    db_path = Path(db_path or DEFAULT_SNAPSHOT)
    out = Path(out_dir or DEFAULT_CORPUS)
    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    try:
        rows = con.execute(_G1_SQL).fetchall()
    finally:
        con.close()

    n = 0
    with open(out / "g1.jsonl", "w", encoding="utf-8") as f:
        for (rid, info, folder, source, artist, y, m, d, stage, pcity, pstate,
             pcountry, vname, vcity, vstate, vcountry, event) in rows:
            if vname and is_placeholder_venue_name(vname):
                vname = None            # a placeholder is "no venue", as everywhere else
            gold = {
                "artist": _nfc(artist),
                "date": {"y": y, "m": m, "d": d},
                "venue": _nfc(vname),
                "event": _nfc(event),
                "stage": _nfc(stage),
                "city": _nfc(pcity or vcity),
                "state": _nfc(pstate or vstate),
                "country": _nfc(pcountry or vcountry),
                "source": _nfc(source),
            }
            item = {"id": str(rid), "info_text": info,
                    "folder_name": _nfc(folder or ""), "gold": gold}
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            n += 1
    meta = {"snapshot_db": str(db_path),
            "snapshot_date": datetime.fromtimestamp(db_path.stat().st_mtime).strftime("%Y-%m-%d"),
            "n": n, "exported": datetime.now().strftime("%Y-%m-%d %H:%M")}
    (out / "g1.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


# ── Predictions ──────────────────────────────────────────────────────────────

def _empty_pred():
    return {f: None for f in FIELDS}


def predict_reader(item):
    """info text alone, through parse_info_file()."""
    from app.utils.ingest import parse_info_file
    r = parse_info_file(None, text=item.get("info_text") or "")
    p = _empty_pred()
    p["artist"] = r.get("artist")
    p["date"] = (r.get("year"), r.get("month"), r.get("day"))
    for k in ("venue", "event", "stage", "city", "state", "country", "source"):
        p[k] = r.get(k)
    return p, None, None


def build_scan(item):
    """
    A minimal scan payload, shaped like build_scan_payload()'s output, from an
    eval item: the info text goes through parse_info_file(), tags exist only
    when the item carries them, and the folder-name fallbacks match scan time.
    """
    from app.utils import ingest as ing
    parsed = ing.parse_info_file(None, text=item.get("info_text") or "")
    folder_name = item.get("folder_name") or ""
    n_audio = int(item.get("n_audio") or 0)
    tags = item.get("tags") or {}

    raw = {k: tags[k] for k in ("artist", "albumartist", "album") if tags.get(k)}
    tag_tracks = [{"index": i, "filename": f"{i:02d}.flac", "raw": dict(raw)}
                  for i in range(1, max(n_audio, 1 if raw else 0) + 1)] if raw else []
    from_tags = {
        "artist": tags.get("artist"), "concert_date": tags.get("concert_date"),
        "venue": tags.get("venue"), "location": tags.get("location"),
        "city": tags.get("city"), "state": tags.get("state"), "country": tags.get("country"),
        "source": tags.get("source"), "lineage": tags.get("lineage"),
        "tracks": tag_tracks,
    }
    keys = ("artist", "artist_match", "year", "month", "day", "venue", "venue_match",
            "city", "state", "country", "source", "lineage")
    from_info = {k: parsed.get(k) for k in keys}
    from_info["tracks"] = [{"number": t["number"], "title": t["title"],
                            "songwriter": t.get("songwriter")}
                           for t in parsed.get("tracks", [])]

    # Same last-resort folder-name fallbacks as build_scan_payload().
    if not from_tags.get("source") and not from_info.get("source"):
        s = ing.detect_source_from_name(folder_name)
        if s:
            from_info["source"] = s
    if not from_tags.get("lineage") and not from_info.get("lineage"):
        gear = ing.detect_gear_from_name(folder_name)
        if gear:
            from_info["lineage"] = ", ".join(gear)
    st = ing.detect_source_tag_from_name(folder_name)
    if st:
        from_info["source_tag"] = st
    shn = ing.detect_shnid_from_name(folder_name)
    if shn:
        from_info["etree_shnid"] = shn

    return {
        "folder_path": None,
        "folder_name": folder_name,
        "audio_file_count": n_audio,
        "audio_files": [{"index": i, "filename": f"{i:02d}.flac"}
                        for i in range(1, n_audio + 1)],
        "info_file_content": item.get("info_text"),
        "suggestions": {"from_tags": from_tags, "from_info_file": from_info},
    }


def predict_resolve(item):
    """build_scan() -> resolve() -> verdict(). Fields resolve() lacks yet read as empty."""
    from app.utils.resolve import resolve, verdict
    res = resolve(build_scan(item))
    status, reasons = verdict(res)

    def val(name):
        f = getattr(res, name, None)
        return getattr(f, "value", None) if f is not None else None

    p = _empty_pred()
    p["artist"] = val("artist")
    d = val("date") or {}
    p["date"] = (d.get("year"), d.get("month"), d.get("day"))
    for k in ("venue", "event", "stage", "city", "state", "country", "source"):
        p[k] = val(k)
    # Confidence arrives in chunk 6; until then every value is "confident".
    conf = {k: (getattr(getattr(res, k, None), "confidence", None) or "confident")
            for k in FIELDS if hasattr(res, k)}
    return p, status, conf


# ── Scoring ──────────────────────────────────────────────────────────────────

def _jsonable(v):
    return list(v) if isinstance(v, tuple) else v


def score_item(item, pred, status=None, conf=None):
    gold = item.get("gold") or {}
    judgments = {f: judge_field(f, gold.get(f), pred.get(f)) for f in FIELDS}
    # A required field that is wrong: artist wrong or only partly right, date wrong.
    # (A partly-right artist still files the recording under the wrong act.)
    auto_wrong = None
    if status is not None:
        auto_wrong = (status == "ingested"
                      and (judgments["artist"] in ("wrong", "partial")
                           or judgments["date"] == "wrong"))
    return {"id": str(item.get("id")), "judgments": judgments,
            "pred": {k: _jsonable(v) for k, v in pred.items()},
            "verdict": status, "conf": conf or {}, "auto_wrong": auto_wrong}


def aggregate(results):
    fields = {}
    for f in FIELDS:
        c = {j: 0 for j in JUDGMENTS}
        no_key = cbw = 0
        for r in results:
            j = r["judgments"][f]
            if j is None:
                no_key += 1
                continue
            c[j] += 1
            if j == "wrong" and (r["conf"].get(f) or "confident") == "confident":
                cbw += 1
        n = sum(c.values())
        answered = c["right"] + c["partial"] + c["wrong"]
        fields[f] = {
            **c, "n": n, "no_key": no_key,
            "precision": c["right"] / answered if answered else None,
            "recall": c["right"] / n if n else None,
            "wrong_rate": c["wrong"] / n if n else None,
            "confident_but_wrong": cbw / n if n else None,
        }
    with_verdict = [r for r in results if r["verdict"] is not None]
    ingested = [r for r in with_verdict if r["verdict"] == "ingested"]
    wrong_rows = [r for r in with_verdict if r["auto_wrong"]]
    auto = {
        "n_with_verdict": len(with_verdict),
        "n_ingested": len(ingested),
        "would_auto_ingest_wrongly": len(wrong_rows),
        # a row can appear under more than one reason
        "by_reason": {
            "artist_wrong": sum(1 for r in wrong_rows if r["judgments"]["artist"] == "wrong"),
            "artist_partial": sum(1 for r in wrong_rows if r["judgments"]["artist"] == "partial"),
            "date_wrong": sum(1 for r in wrong_rows if r["judgments"]["date"] == "wrong"),
        },
    }
    auto["rate_of_all"] = (auto["would_auto_ingest_wrongly"] / len(with_verdict)
                           if with_verdict else None)
    auto["rate_of_ingested"] = (auto["would_auto_ingest_wrongly"] / len(ingested)
                                if ingested else None)
    return {"fields": fields, "auto_ingest": auto}


def run_tier(tier, mode="reader", corpus_dir=None, fixtures_dir=None, limit=None):
    """Score every item in a tier. mode is 'reader' or 'resolve'."""
    items, meta = load_tier(tier, corpus_dir, fixtures_dir)
    if limit:
        items = items[:limit]
    predict = predict_resolve if mode == "resolve" else predict_reader
    results, times = [], []
    for it in items:
        t0 = time.perf_counter()
        try:
            pred, status, conf = predict(it)
        except Exception as exc:                      # a crash is a result, not a halt
            pred, status, conf = _empty_pred(), None, None
            pred["_error"] = f"{type(exc).__name__}: {exc}"
        times.append((time.perf_counter() - t0) * 1000)
        results.append(score_item(it, pred, status, conf))
    errors = sum(1 for r in results if "_error" in r["pred"])
    ms = sorted(times)
    timing = {"p50_ms": statistics.median(ms) if ms else None,
              "p95_ms": ms[min(len(ms) - 1, int(len(ms) * 0.95))] if ms else None}
    return {"tier": tier, "mode": mode, "n": len(items), "errors": errors,
            "meta": meta, "timing": timing, "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
            **aggregate(results), "items": results}


# ── Report and diff ──────────────────────────────────────────────────────────

def _pct(x):
    return "  -  " if x is None else f"{x * 100:5.1f}"


def format_report(rep):
    snap = rep["meta"].get("snapshot_date")
    head = (f"{rep['tier']} ({rep['mode']})  n={rep['n']}"
            + (f"  snapshot {snap}" if snap else "") + f"  run {rep['when']}")
    out = [head]
    if rep["errors"]:
        out.append(f"!! {rep['errors']} item(s) raised; they score as empty")
    if rep["mode"] == "resolve" and rep["tier"] == "G1":
        out.append("note: folder_name is the original folder name and may state a date or source")
    out.append("")
    out.append(f"{'field':<9}{'n':>5}{'right':>7}{'part':>6}{'wrong':>7}{'empty':>7}"
               f"{'nokey':>7}{'prec%':>7}{'rec%':>7}{'CBW%':>7}")
    for f in FIELDS:
        s = rep["fields"][f]
        out.append(f"{f:<9}{s['n']:>5}{s['right']:>7}{s['partial']:>6}{s['wrong']:>7}"
                   f"{s['empty']:>7}{s['no_key']:>7}{_pct(s['precision']):>7}"
                   f"{_pct(s['recall']):>7}{_pct(s['confident_but_wrong']):>7}")
    a = rep["auto_ingest"]
    if a["n_with_verdict"]:
        out.append("")
        out.append(f"would-auto-ingest-wrongly: {a['would_auto_ingest_wrongly']} of "
                   f"{a['n_with_verdict']} ({_pct(a['rate_of_all']).strip()}%); "
                   f"{a['n_ingested']} would ingest")
        br = a["by_reason"]
        out.append(f"  by reason: artist wrong {br['artist_wrong']}, artist partial "
                   f"{br['artist_partial']}, date wrong {br['date_wrong']}")
    t = rep["timing"]
    if t["p50_ms"] is not None:
        out.append(f"time per item: p50 {t['p50_ms']:.2f} ms, p95 {t['p95_ms']:.2f} ms")
    out.append("CBW = wrong and confident, over scored items (all values are confident until chunk 6)")
    return "\n".join(out)


def _run_key(rep):
    return f"{rep['tier']}:{rep['mode']}"


def load_last_run(corpus_dir=None):
    p = Path(corpus_dir or DEFAULT_CORPUS) / LAST_RUN
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def diff_runs(old, new):
    """Items whose judgment changed, per field: [(id, field, old, new, pred)]."""
    if not old:
        return None
    before = {r["id"]: r for r in old["items"]}
    changes = []
    for r in new["items"]:
        o = before.get(r["id"])
        if not o:
            continue
        for f in FIELDS:
            if o["judgments"].get(f) != r["judgments"][f]:
                changes.append((r["id"], f, o["judgments"].get(f), r["judgments"][f],
                                r["pred"].get(f)))
    return changes


def format_diff(changes):
    if changes is None:
        return "diff: no previous run for this tier and mode"
    if not changes:
        return "diff: no item changed verdict on any field"
    better = {("wrong", "right"), ("partial", "right"), ("empty", "right"),
              ("wrong", "partial"), ("wrong", "empty")}
    out = [f"diff: {len(changes)} change(s)"]
    for rid, f, o, n, pred in sorted(changes, key=lambda c: (c[1], c[0])):
        arrow = "+" if (o, n) in better else "-" if (n, o) in better else "~"
        out.append(f"  {arrow} {f:<8} item {rid:<8} {o} -> {n}   now: {pred}")
    return "\n".join(out)


def save_last_run(rep, corpus_dir=None):
    d = Path(corpus_dir or DEFAULT_CORPUS)
    d.mkdir(parents=True, exist_ok=True)
    data = load_last_run(d)
    data[_run_key(rep)] = rep
    (d / LAST_RUN).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m app.utils.resolver_eval",
                                 description="Resolver evaluation harness")
    ap.add_argument("--tier", choices=TIERS, default="G4")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--reader-only", action="store_true", help="info text alone (default)")
    mode.add_argument("--resolve", action="store_true", help="scan payload through resolve()")
    ap.add_argument("--diff", action="store_true", help="print items that changed verdict since the last run")
    ap.add_argument("--corpus", help="corpus folder (default ~/Workshop/dev/resolver-corpus)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-write", action="store_true", help="do not update last_run.json")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--export-g1", action="store_true", help="write corpus/g1.jsonl from the snapshot")
    ap.add_argument("--db", help="snapshot path for --export-g1 (default db/_cowork.db)")
    args = ap.parse_args(argv)

    if args.export_g1:
        meta = export_g1(args.db, args.corpus)
        print(f"g1.jsonl: {meta['n']} items from snapshot dated {meta['snapshot_date']}")
        return 0

    rep = run_tier(args.tier, "resolve" if args.resolve else "reader",
                   corpus_dir=args.corpus, limit=args.limit)
    changes = diff_runs(load_last_run(args.corpus).get(_run_key(rep)), rep) if args.diff else None
    if args.json:
        print(json.dumps({k: v for k, v in rep.items() if k != "items"}, indent=2))
    else:
        print(format_report(rep))
    if args.diff:
        print()
        print(format_diff(changes))
    if not args.no_write:
        save_last_run(rep, args.corpus)
    return 0


if __name__ == "__main__":
    sys.exit(main())
