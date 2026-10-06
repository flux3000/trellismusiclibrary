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
JUDGMENTS = ("right", "variant", "partial", "wrong", "empty")

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


# Ensemble and billing variants of one act (Ryan, 2026-10-04): "Miles Davis" and "Miles Davis
# Quintet", "Dave Matthews" and "Dave Matthews Band", "Bob Weir and Friends", "Phil Lesh with
# Warren Haynes". Unattended import still files each spelling under its own Artist row; the
# harness only stops counting the difference as an error.
_VARIANT_TAIL = re.compile(
    r"(?:\s+(?:trio|quartet|quintet|sextet|septet|octet|band|group|orchestra))+$"
    r"|\s+and\s+friends$|\s+and\s+(?:his|her)\s+.+$|\s+with\s+.+$")


def _variant_base(s):
    return _VARIANT_TAIL.sub("", s).strip()


def is_variant(gold, pred):
    g, p = norm(gold), norm(pred)
    return bool(g) and bool(p) and g != p and _variant_base(g) == _variant_base(p) != ""


def judge_artist(gold, pred):
    j = judge_text(gold, pred)
    if j in ("partial", "wrong") and gold is not False and is_variant(gold, pred):
        return "variant"
    return j


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
    if field == "artist":
        return judge_artist(gold, pred)
    if field == "state":
        return judge_text(gold, pred, _canon_state)
    if field == "country":
        return judge_text(gold, pred, _canon_country)
    return judge_text(gold, pred)


# ── Archive tiers (chunk 7): G3 catalog key, G2 metadata key ─────────────────
# The key is not in Trellis's field shapes, so the place fields are scored against the
# key's own strings. G3: venue and location are one string ("Venue, City, ST", sometimes
# "Event, Venue, City, ST"). G2: free-text venue and coverage, uploader-typed and noisy.

def _act_key(name):
    from app.utils.artists import _act_key as k
    return k(name)


_ART_RANK = {"right": 0, "variant": 1, "partial": 2, "wrong": 3, "empty": 4, None: 5}


def judge_artist_archive(gold, pred):
    """judge_artist, plus: the same act after _act_key normalisation counts as a variant."""
    # The catalog heading can name several billings of one act: "Del McCoury / Del McCoury Band".
    parts = [x.strip() for x in str(gold).split(" / ")] if gold and " / " in str(gold) else [gold]
    best = None
    for part in parts:
        j = judge_artist(part, pred)
        if j in ("partial", "wrong") and part is not False:
            g, p = _act_key(part), _act_key(pred)
            if g and p and (g == p or g.replace(" ", "") == p.replace(" ", "")):
                j = "variant"      # "Mc Govern String Band" is "McGovern String Band" (2026-10-05)
        if best is None or _ART_RANK.get(j, 9) < _ART_RANK.get(best, 9):
            best = j
    return best


# Spelling variants of the same venue word that the catalog and a taper's text disagree on
# (Theater/Theatre, Center/Centre, Fairground/Fairgrounds). Scoring only: not a reader rule.
_VENUE_WORDS = {"theatre": "theater", "centre": "center", "grounds": "ground", "fairgrounds": "fairground",
                "ft": "fort", "mtn": "mountain", "mt": "mount",
                "st": "saint", "ctr": "center", "ave": "avenue"}      # 2026-10-05: plain abbreviations the 20-row G3 check found scored wrong


def _vnorm(s):
    return " ".join(_VENUE_WORDS.get(w, w) for w in norm(s).split())


def _parts(raw, canon=norm):
    return [x for x in (canon(p) for p in re.split(r"[,;]", str(raw or ""))) if x]


def judge_catalog_venue(raw, venue, event=None, city=None, state=None):
    """
    G3 venue. right: venue + location, normalised as one string, equals the key (in any of
    the natural orders), or the resolved venue or the resolved event equals one comma part
    of it (event-plus-venue strings). partial: words contained. None: the key is empty.
    """
    gold_full = _vnorm(raw)
    if not gold_full:
        return None
    pv, pe = _vnorm(venue), _vnorm(event)
    if not pv and not pe:
        return "empty"
    parts = _parts(raw, _vnorm)
    # "DelFest" and "Del Fest" are one name: compare with the spaces removed too (2026-10-05)
    sq = lambda t: t.replace(" ", "")
    if (pv and sq(pv) in {sq(p) for p in parts}) or (pe and sq(pe) in {sq(p) for p in parts}):
        return "right"
    for combo in ((venue, city, state), (event, venue, city, state), (event, venue),
                  (venue, city), (event, city, state)):
        joined = _vnorm(", ".join(str(x) for x in combo if x))
        if joined and joined == gold_full:
            return "right"
    if pv in parts or pe in parts:
        return "right"
    for x in (pv, pe):
        if x and (_has_words(x, gold_full) or any(_has_words(p, x) for p in parts)):
            return "partial"
    return "wrong"


def _state_tokens(raw):
    toks = {_canon_state(x) for x in re.split(r"[,;]", str(raw or "")) if x.strip()}
    toks |= {_canon_state(w) for w in norm(raw).split()}
    return toks


def judge_in_string(field, pred, haystack, needs_location_part=True):
    """city or state appears in the key's string. right / wrong / empty; None when the key
    gives nothing to check against (G3: no location part, so no city or state to find)."""
    h = norm(haystack)
    if not h:
        return None
    p = norm(pred)
    if field == "state":
        toks = _state_tokens(haystack)
        if needs_location_part and not (toks & set(_US_STATES.values())):
            return None
        if not p:
            return "empty"
        return "right" if _canon_state(pred) in toks else "wrong"
    if p and _has_words(p, h):
        return "right"
    if needs_location_part and len(_parts(haystack)) < 2:
        return None
    return "empty" if not p else "wrong"


def judge_loose_venue(gold, pred):
    """G2 venue: equal -> right; one contains the other, or half the words of the shorter
    one are shared -> partial; else wrong. Loose on purpose: the key is noisy."""
    g, p = norm(gold), norm(pred)
    if not g:
        return None
    if not p:
        return "empty"
    if g == p:
        return "right"
    if _has_words(g, p) or _has_words(p, g):
        return "partial"
    gw, pw = set(g.split()), set(p.split())
    if gw and pw and len(gw & pw) / min(len(gw), len(pw)) >= 0.5:
        return "partial"
    return "wrong"


def judge_date_archive(gold, pred, century_inferred=False):
    """judge_date; when the key's century was only inferred (00-26 -> 20xx), a prediction
    that differs from it by the century alone is not scorable (the key may be the wrong one)."""
    j = judge_date(gold, pred)
    if j == "wrong" and century_inferred:
        g, p = _date_tuple(gold), _date_tuple(pred)
        if g and p and p[0] and g[0] % 100 == p[0] % 100 and g[1:] == p[1:]:
            return None
    return j


def judge_item(tier, item, pred):
    """Per-field judgments. G1/G4 use judge_field; G2 and G3 use the archive rules above."""
    gold = item.get("gold") or {}
    j = {f: judge_field(f, gold.get(f), pred.get(f)) for f in FIELDS}
    if tier not in ("G2", "G3"):
        return j
    j["artist"] = judge_artist_archive(gold.get("artist"), pred.get("artist"))
    infer = bool((item.get("archive_meta") or {}).get("century_inferred"))
    j["date"] = judge_date_archive(gold.get("date"), pred.get("date"), infer)
    # The corpus convention folds a festival-only file into `venue` (venue_or_event), so the
    # resolved event is offered to the G3 rule as a separate string and nothing else changes.
    pv = pred.get("venue")
    ev = pred.get("event")
    if tier == "G3" and gold.get("venue_raw"):
        raw = gold["venue_raw"]
        j["venue"] = judge_catalog_venue(raw, pv, ev, pred.get("city"), pred.get("state"))
        j["city"] = judge_in_string("city", pred.get("city"), raw)
        j["state"] = judge_in_string("state", pred.get("state"), raw)
        # The catalog's source is a lineage chain ("SBD>Dat>CD>EAC>Flac"), not Trellis's source
        # type (SBD, AUD, ...): the two never compare, so the key says nothing about this field.
        j["source"] = None
    elif tier == "G2":
        j["venue"] = judge_loose_venue(gold.get("venue"), pv)
        cov = gold.get("coverage_raw")
        j["city"] = judge_in_string("city", pred.get("city"), cov, needs_location_part=False) if cov else None
        j["state"] = judge_in_string("state", pred.get("state"), cov, needs_location_part=False) if cov else None
    return j


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
        raise FileNotFoundError(f"{path} not found (G1: run --export-g1; G2/G3: python3 -m app.utils.resolver_corpus g2|g3)")
    meta = {"source": str(path)}
    meta_path = corpus / f"{tier.lower()}.meta.json"
    if meta_path.exists():
        meta.update(json.loads(meta_path.read_text(encoding="utf-8")))
    items = _read_jsonl(path)
    if tier == "G3":
        for it in items:
            repair_catalog_artist(it)
    return items, meta


def repair_catalog_artist(item):
    """The catalog heading of some acts lost its first one to four letters ("y Strings" for
    Billy Strings, "rew Emmitt Band / ..." for Drew Emmitt Band). The folder the show sits in
    is named for the act and carries the whole name: a heading part that is a mid-word tail of
    that folder's name is put back. Scoring and fitting both read the repaired name; the
    original stays in archive_meta."""
    meta = item.get("archive_meta") or {}
    folder = str(meta.get("artist_dir") or "").replace("&", "and")
    gold = item.get("gold") or {}
    parts = str(gold.get("artist") or "").split(" / ")
    fl = folder.lower()
    out = []
    for p in parts:
        pl = p.lower()
        cut = len(fl) - len(pl)
        if p and 1 <= cut <= 4 and fl.endswith(pl) and fl[cut - 1].isalpha() and fl[cut].isalpha():
            p = folder[:cut] + p
        out.append(p)
    fixed = " / ".join(out)
    if fixed != gold.get("artist"):
        meta["artist_catalog_heading"] = gold.get("artist")
        gold["artist"] = fixed
    return item


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


# ── Library modes ────────────────────────────────────────────────────────────
# "none": the reader sees an empty library. "loo": leave-one-out, the item's own
# performance's venue/event/act rows count only if another performance also uses
# them. There is deliberately no mode that scores with the full library.
LIBRARY_MODES = ("none", "loo")
_LIB = {"mode": "none", "full": None, "rec_perf": None, "db": None, "corpus": None,
        "g3": None, "g3_pid": None, "g3_confirmed": True}

_STATE_CODE = re.compile(r"^[A-Za-z]{2}$")


def set_g3_confirmed(flag):
    """G3 leave-one-out: treat the other shows' artists as confirmed (simulates one reviewed
    show per act) or not at all. G1 library rows always count as confirmed (a person saved them)."""
    _LIB["g3_confirmed"] = bool(flag)
    _LIB["g3"] = _LIB["g3_pid"] = None


def set_library_mode(mode, db=None, corpus_dir=None):
    if mode not in LIBRARY_MODES:
        raise ValueError(f"library mode must be one of {LIBRARY_MODES}")
    _LIB["mode"] = mode
    _LIB["db"] = db
    _LIB["corpus"] = corpus_dir
    _LIB["full"] = _LIB["rec_perf"] = _LIB["g3"] = _LIB["g3_pid"] = None


def _g3_library(corpus_dir):
    """The G3 leave-one-out source: one Artist row per gold act and one Venue row per
    (name, city, state) read from the catalog's own "Venue, City, ST" string, one
    performance per show. The catalog string is split generically (first comma part is the
    name, the last is a state when it is two letters, the one before it the city); nothing
    here is keyed on a particular site beyond the corpus's own gold shape."""
    from app.utils.reader.library import LibraryIndex
    items, _ = load_tier("G3", corpus_dir)
    artists, venues, perfs, pid_of = {}, {}, [], {}
    for n, it in enumerate(items, 1):
        g = it.get("gold") or {}
        a = str(g.get("artist") or "").split(" / ")[0].strip()
        aid = artists.setdefault(norm(a), (len(artists) + 1, a))[0] if a else None
        vid = None
        parts = [x.strip() for x in str(g.get("venue_raw") or "").split(",") if x.strip()]
        if parts:
            state = parts[-1] if len(parts) >= 2 and _STATE_CODE.match(parts[-1]) else None
            city = parts[-2] if state and len(parts) >= 3 else None
            key = (norm(parts[0]), norm(city), norm(state))
            vid = venues.setdefault(key, (len(venues) + 1, parts[0], city, state, "US" if state else None))[0]
        perfs.append((n, aid, vid, None))
        pid_of[str(it.get("id"))] = n
    ix = LibraryIndex.from_rows(list(artists.values()), [], [], list(venues.values()), [], perfs)
    ix.confirmed = frozenset(ix.artists) if _LIB["g3_confirmed"] else frozenset()
    return ix, pid_of


def library_for(item):
    """The LibraryIndex a prediction for `item` may see."""
    from app.utils.reader.library import LibraryIndex
    if _LIB["mode"] != "loo":
        return LibraryIndex.empty()
    if (item.get("archive_meta") or {}).get("tier") == "G3":
        if _LIB["g3"] is None:
            _LIB["g3"], _LIB["g3_pid"] = _g3_library(_LIB["corpus"])
        pid = _LIB["g3_pid"].get(str(item.get("id")))
        return _LIB["g3"].without_performance(pid) if pid is not None else LibraryIndex.empty()
    if _LIB["full"] is None:
        import sqlite3
        path = _LIB["db"] or str(Path(__file__).resolve().parents[2] / "db" / "_cowork.db")
        _LIB["full"] = LibraryIndex.from_snapshot(path)
        _LIB["full"].confirmed = frozenset(_LIB["full"].artists)     # every saved act counts as confirmed
        con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            _LIB["rec_perf"] = {str(r[0]): r[1] for r in con.execute("select id, performance_id from recording")}
        finally:
            con.close()
    pid = _LIB["rec_perf"].get(str(item.get("id")))
    return _LIB["full"].without_performance(pid) if pid is not None else LibraryIndex.empty()


ATLAS_MODES = ("none", "fixture", "real")      # or a path to an atlas.sqlite
_ATLAS = {"mode": "none", "tmp": None}


def set_atlas_mode(mode):
    """none: no Atlas (default, so numbers never depend on a local file); fixture: the tiny test Atlas;
    real: the app's own (assets/atlas/atlas.sqlite or $TRELLIS_ATLAS); or a path to an atlas.sqlite."""
    from app.atlas import lookup
    _ATLAS["mode"] = mode
    if mode == "none":
        lookup.set_atlas(None)
    elif mode == "fixture":
        import tempfile
        from tests.fixtures.atlas_fixture import build_fixture
        if _ATLAS["tmp"] is None:
            _ATLAS["tmp"] = tempfile.mkdtemp(prefix="atlas-fixture-")
        path = str(Path(_ATLAS["tmp"]) / "atlas.sqlite")
        build_fixture(path)
        lookup.set_atlas(path)
    elif mode == "real":
        lookup.reset_atlas()
    else:
        lookup.set_atlas(mode)


def _parse(item):
    from app.utils.ingest import parse_info_file
    from app.atlas.lookup import current_atlas
    return parse_info_file(None, text=item.get("info_text") or "", library=library_for(item),
                           n_audio=int(item.get("n_audio") or 0) or None,
                           atlas=current_atlas() if _ATLAS["mode"] != "none" else None)


def venue_or_event(r):
    """Corpus convention: a festival-only file carries the festival as its venue
    (gold in G1 to G3 was written that way). Resolved has its own event field since
    chunk 4, so the convention now lives only here, in how a prediction is scored."""
    return r.get("venue") or r.get("event")


def predict_reader(item):
    """info text alone, through parse_info_file()."""
    r = _parse(item)
    p = _empty_pred()
    p["artist"] = r.get("artist")
    p["date"] = (r.get("year"), r.get("month"), r.get("day"))
    for k in ("venue", "event", "stage", "city", "state", "country", "source"):
        p[k] = r.get(k)
    p["venue"] = venue_or_event(r)
    return p, None, None, None


def build_scan(item):
    """
    A minimal scan payload, shaped like build_scan_payload()'s output, from an
    eval item: the info text goes through parse_info_file(), tags exist only
    when the item carries them, and the folder-name fallbacks match scan time.
    """
    from app.utils import ingest as ing
    parsed = _parse(item)
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
            "city", "state", "country", "source", "lineage", "event", "stage")
    from_info = {k: parsed.get(k) for k in keys}
    from_info["tracks"] = [{"number": t["number"], "title": t["title"],
                            "songwriter": t.get("songwriter")}
                           for t in parsed.get("tracks", [])]
    from_info["evidence"] = parsed.get("evidence")

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


def _resolve_with_library(item, scan):
    """resolve() as the app runs it: the library it consults (a known act, a known venue) is the
    one the reader was given for this item, so a leave-one-out tier stays leave-one-out here too."""
    from app.utils.resolve import resolve
    lib = library_for(item)
    if lib is None or getattr(lib, "is_empty", True):
        return resolve(scan)
    from app.utils.reader import library as L
    saved = L.current_library
    L.current_library = lambda: lib
    try:
        return resolve(scan)
    finally:
        L.current_library = saved


def predict_resolve(item):
    """build_scan() -> resolve() -> verdict(). Fields resolve() lacks yet read as empty."""
    from app.utils.resolve import verdict
    res = _resolve_with_library(item, build_scan(item))
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
    p["venue"] = p["venue"] or p["event"]          # corpus convention, as in predict_reader
    conf = {k: (getattr(getattr(res, k, None), "confidence", None) or "confident")
            for k in FIELDS if hasattr(res, k)}
    # The raw scores the confidence came from, for fitting thresholds (reader/calibrate.py).
    from app.utils.reader.confidence import CALIBRATED_FIELDS
    assess = {}
    for k in CALIBRATED_FIELDS:
        f = getattr(res, k)
        assess[k] = {"logit": f.logit, "margin": f.margin, "has_value": f.value is not None}
        if k == "date":
            assess[k]["srcs"] = sorted((f.candidates or {}).keys())
    assess["_reasons"] = list(reasons)
    return p, status, conf, assess


# ── Scoring ──────────────────────────────────────────────────────────────────

def _jsonable(v):
    return list(v) if isinstance(v, tuple) else v


# A value is "wrong" for the confident-but-wrong gate when it would be filed wrongly: an
# artist that is only partly right still files the recording under the wrong act.
WRONG_FOR = {"artist": ("wrong", "partial")}


def is_wrong(field, judgment):
    return judgment in WRONG_FOR.get(field, ("wrong",))


def score_item(item, pred, status=None, conf=None, assess=None, tier=None):
    judgments = judge_item(tier, item, pred)
    # A required field that is wrong: artist wrong or only partly right, date wrong.
    # (A partly-right artist still files the recording under the wrong act.)
    auto_wrong = None
    if status is not None:
        auto_wrong = (status == "ingested"
                      and (judgments["artist"] in ("wrong", "partial")
                           or judgments["date"] == "wrong"))
    return {"id": str(item.get("id")), "judgments": judgments,
            "pred": {k: _jsonable(v) for k, v in pred.items()},
            "verdict": status, "conf": conf or {}, "auto_wrong": auto_wrong,
            "assess": assess or {}}


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
            if is_wrong(f, j) and (r["conf"].get(f) or "confident") == "confident":
                cbw += 1
        n = sum(c.values())
        good = c["right"] + c["variant"]
        answered = good + c["partial"] + c["wrong"]
        fields[f] = {
            **c, "n": n, "no_key": no_key,
            "precision": good / answered if answered else None,
            "recall": good / n if n else None,
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


def run_tier(tier, mode="reader", corpus_dir=None, fixtures_dir=None, limit=None, library="none",
             atlas="none", only_ids=None):
    """Score every item in a tier. mode is 'reader' or 'resolve'; library is 'none' or 'loo'.
    only_ids restricts the run to those item ids (the held-out fold of a cross-validation)."""
    set_library_mode(library, corpus_dir=corpus_dir)
    set_atlas_mode(atlas)
    items, meta = load_tier(tier, corpus_dir, fixtures_dir)
    if only_ids is not None:
        items = [it for it in items if str(it.get("id")) in only_ids]
    if limit:
        items = items[:limit]
    predict = predict_resolve if mode == "resolve" else predict_reader
    results, times = [], []
    for it in items:
        t0 = time.perf_counter()
        try:
            pred, status, conf, assess = predict(it)
        except Exception as exc:                      # a crash is a result, not a halt
            pred, status, conf, assess = _empty_pred(), None, None, None
            pred["_error"] = f"{type(exc).__name__}: {exc}"
        times.append((time.perf_counter() - t0) * 1000)
        results.append(score_item(it, pred, status, conf, assess, tier=tier))
    errors = sum(1 for r in results if "_error" in r["pred"])
    ms = sorted(times)
    q = lambda f: ms[min(len(ms) - 1, int(len(ms) * f))] if ms else None
    timing = {"p50_ms": statistics.median(ms) if ms else None, "p95_ms": q(0.95),
              "p90_ms": q(0.90), "p99_ms": q(0.99), "max_ms": ms[-1] if ms else None,
              "mean_ms": statistics.fmean(ms) if ms else None,
              "slowest": sorted(((t, i) for i, t in enumerate(times)), reverse=True)[:5]}
    return {"tier": tier, "mode": mode, "library": library, "atlas": str(atlas), "n": len(items), "errors": errors,
            "meta": meta, "timing": timing, "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
            **aggregate(results), "items": results}


# ── Calibration (chunk 6) ────────────────────────────────────────────────────

CAL_POPS = ("none", "loo")


def _artist_group(item):
    return norm((item.get("gold") or {}).get("artist")) or f"item-{item.get('id')}"


def collect_samples(corpus_dir=None, atlas="real", pops=CAL_POPS):
    """One sample per (item, population, field) with a gold value: the raw logit and
    margin the resolver computed, and whether the value is wrong."""
    from app.utils.reader.confidence import CALIBRATED_FIELDS
    items, meta = load_tier("G1", corpus_dir)
    by_id = {str(it["id"]): it for it in items}
    samples = {f: [] for f in CALIBRATED_FIELDS}
    for pop in pops:
        rep = run_tier("G1", "resolve", corpus_dir=corpus_dir, library=pop, atlas=atlas)
        for r in rep["items"]:
            group = _artist_group(by_id[r["id"]])
            for f in CALIBRATED_FIELDS:
                j = r["judgments"][f]
                if j is None:
                    continue
                a = r["assess"].get(f) or {}
                has = bool(a.get("has_value"))
                samples[f].append({"item": r["id"], "group": group, "pop": pop,
                                   "logit": a.get("logit") if has else 0.0,
                                   "margin": a.get("margin") if has else 0.0,
                                   "has_value": has, "wrong": is_wrong(f, j)})
    return samples, items, meta


def calibrate(corpus_dir=None, atlas="real", k=5, train_target=None, write=False):
    """Fit the thresholds on G1 with k folds by artist, replay each fold's held-out items
    through resolve() + verdict() with the model fitted on the other folds, and return the
    held-out report. With write=True the all-of-G1 fit goes to reader/calibration.json."""
    from app.utils.reader import calibrate as cal
    from app.utils.reader import confidence as conf
    train_target = cal.TRAIN_TARGET if train_target is None else train_target
    samples, items, meta = collect_samples(corpus_dir, atlas)
    folds = cal.assign_folds([_artist_group(it) for it in items], k)
    notes = ["tags and folder-artist evidence (reader/confidence.py TAGS_SCORE, FOLDER_ARTIST_SCORE, "
             "DATE_WEIGHT['tags']): G1 has no tags and no artist folders, so those weights are set, not fitted",
             "the info-text date and the folder-name date are both exercised (G1 carries the original folder names)"]
    doc, fold_models = cal.build_calibration(
        samples, folds, k=k, train_target=train_target, snapshot=meta.get("snapshot_date"),
        n_items=len(items), populations=[f"resolve, library {p}, real Atlas" for p in CAL_POPS], notes=notes)

    held = {}
    try:
        for f in range(k):
            ids = {str(it["id"]) for it in items if folds[_artist_group(it)] == f}
            fold_cal = {"fields": {name: models[f] for name, models in fold_models.items()}}
            conf.set_calibration(fold_cal)
            for pop in CAL_POPS:
                rep = run_tier("G1", "resolve", corpus_dir=corpus_dir, library=pop, atlas=atlas, only_ids=ids)
                held.setdefault(pop, []).extend(rep["items"])
    finally:
        conf.set_calibration(None)
    held_agg = {pop: aggregate(res) for pop, res in held.items()}
    held_bd = {pop: conf_breakdown(res) for pop, res in held.items()}
    held_fo = {pop: folder_only_dates(res) for pop, res in held.items()}
    if write:
        cal.write_calibration(doc)
        conf.load_calibration(force=True)
    return {"doc": doc, "held_out": held_agg, "held_breakdown": held_bd, "folder_only": held_fo, "n": len(items), "folds": k,
            "train_target": train_target}


def review_reasons(results):
    import collections
    c = collections.Counter()
    for r in results:
        if r["verdict"] is None or r["verdict"] == "ingested":
            continue
        for x in (r.get("assess") or {}).get("_reasons") or []:
            c[x] += 1
    return c


def folder_only_dates(results):
    """The class of items whose only date evidence is the folder name: how many, how many
    came out confident, and how many of those confident dates were wrong."""
    c = {"n": 0, "confident": 0, "confident_wrong": 0, "wrong": 0, "right": 0}
    for r in results:
        a = (r.get("assess") or {}).get("date") or {}
        if a.get("srcs") != ["folder"]:
            continue
        j = r["judgments"]["date"]
        if j is None:
            continue
        c["n"] += 1
        c["wrong"] += is_wrong("date", j)
        c["right"] += j == "right"
        if (r["conf"].get("date") or "empty") == "confident":
            c["confident"] += 1
            c["confident_wrong"] += is_wrong("date", j)
    return c


def conf_breakdown(results):
    """{field: {confident, tentative, empty}} over every item of a run."""
    from app.utils.reader.confidence import CALIBRATED_FIELDS
    out = {}
    for f in CALIBRATED_FIELDS:
        c = {"confident": 0, "tentative": 0, "empty": 0}
        for r in results:
            c[(r["conf"].get(f) or "empty")] += 1
        out[f] = c
    return out


def format_calibration(rep):
    from app.utils.reader.confidence import CALIBRATED_FIELDS
    out = [f"calibration  G1 n={rep['n']}  folds {rep['folds']} by artist  fit target "
           f"{rep['train_target'] * 100:.3f}%  gate 0.500%"]
    for pop, agg in rep["held_out"].items():
        bd = rep["held_breakdown"][pop]
        n = rep["n"]
        out += ["", f"held-out, resolve, library {pop}",
                f"{'field':<9}{'conf%':>7}{'tent%':>7}{'empty%':>8}{'scored':>8}{'CBW':>5}{'CBW%':>8}"]
        for f in CALIBRATED_FIELDS:
            s = agg["fields"][f]
            cbw = round((s["confident_but_wrong"] or 0) * s["n"])
            out.append(f"{f:<9}{bd[f]['confident'] / n * 100:>7.1f}{bd[f]['tentative'] / n * 100:>7.1f}"
                       f"{bd[f]['empty'] / n * 100:>8.1f}{s['n']:>8}{cbw:>5}{_pct(s['confident_but_wrong']):>8}")
        fo = rep["folder_only"][pop]
        out.append(f"folder-only date: {fo['n']} items, {fo['right']} right, {fo['wrong']} wrong; "
                   f"{fo['confident']} confident, {fo['confident_wrong']} confident-but-wrong")
        a = agg["auto_ingest"]
        out.append(f"would-auto-ingest-wrongly {a['would_auto_ingest_wrongly']} of {a['n_with_verdict']} "
                   f"({_pct(a['rate_of_all']).strip()}%); review {a['n_with_verdict'] - a['n_ingested']} "
                   f"({(1 - a['n_ingested'] / a['n_with_verdict']) * 100:.1f}%)")
    out += ["", "shipped thresholds (fit on all of G1)"]
    for f in CALIBRATED_FIELDS:
        m = rep["doc"]["fields"][f]
        out.append(f"  {f:<8} tau {m['tau']:.3f}  m {m['m']:.1f}")
    return "\n".join(out)


# ── Report and diff ──────────────────────────────────────────────────────────

def _pct(x):
    return "  -  " if x is None else f"{x * 100:5.1f}"


def format_report(rep):
    snap = rep["meta"].get("snapshot_date")
    head = (f"{rep['tier']} ({rep['mode']}{'' if rep.get('library', 'none') == 'none' else ', library ' + rep['library']}"
            f"{'' if rep.get('atlas', 'none') == 'none' else ', atlas ' + rep['atlas']})  n={rep['n']}"
            + (f"  snapshot {snap}" if snap else "") + f"  run {rep['when']}")
    out = [head]
    if rep["errors"]:
        out.append(f"!! {rep['errors']} item(s) raised; they score as empty")
    if rep["tier"] == "G2":
        out.append("note: G2 is NOISY. The key is Internet Archive metadata typed by uploaders; "
                   "artist and date compared exactly, venue and coverage loosely. Not comparable with G1 or G3.")
    if rep["tier"] == "G3":
        out.append("note: G3 key is the bluegrassarchive.com catalog. Venue and location are scored as one "
                   "string; city and state are checked as appearing in it; inferred-century dates are not scored.")
    if rep["mode"] == "resolve" and rep["tier"] == "G1":
        out.append("note: folder_name is the original folder name and may state a date or source")
    out.append("")
    out.append(f"{'field':<9}{'n':>5}{'right':>7}{'vrnt':>6}{'part':>6}{'wrong':>7}{'empty':>7}"
               f"{'nokey':>7}{'prec%':>7}{'rec%':>7}{'CBW%':>7}")
    for f in FIELDS:
        s = rep["fields"][f]
        out.append(f"{f:<9}{s['n']:>5}{s['right']:>7}{s['variant']:>6}{s['partial']:>6}{s['wrong']:>7}"
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
    if a["n_with_verdict"]:
        rc = review_reasons(rep["items"])
        if rc:
            out.append("  review reasons (an item can have several): "
                       + ", ".join(f"{k} {v}" for k, v in rc.most_common()))
    t = rep["timing"]
    if t["p50_ms"] is not None:
        out.append(f"time per item: p50 {t['p50_ms']:.2f} ms, p95 {t['p95_ms']:.2f} ms")
    out.append("CBW = wrong and confident, over scored items (artist: wrong or partial)")
    rv = rep["auto_ingest"]
    if rv["n_with_verdict"]:
        out.append(f"review rate: {rv['n_with_verdict'] - rv['n_ingested']} of {rv['n_with_verdict']} "
                   f"({(1 - rv['n_ingested'] / rv['n_with_verdict']) * 100:.1f}%)")
    return "\n".join(out)


def format_timing(rep, items_label=lambda i: str(i)):
    """--timing: the whole latency distribution and the slowest items, so a p95 can be
    reproduced and compared (same Atlas file, same library mode, warm cache after pass 1)."""
    import sqlite3
    t = rep["timing"]
    out = [f"timing  {rep['tier']} {rep['mode']}  n={rep['n']}  library {rep.get('library')}  atlas {rep.get('atlas')}",
           f"  SQLite {sqlite3.sqlite_version}",
           f"  mean {t['mean_ms']:.2f}  p50 {t['p50_ms']:.2f}  p90 {t['p90_ms']:.2f}  p95 {t['p95_ms']:.2f}  "
           f"p99 {t['p99_ms']:.2f}  max {t['max_ms']:.2f}  (ms per item)"]
    out += [f"  slowest: item {i} {ms:.1f} ms" for ms, i in t["slowest"]]
    return "\n".join(out)


def _run_key(rep):
    lib = rep.get("library", "none")
    atl = rep.get("atlas", "none")
    return f"{rep['tier']}:{rep['mode']}" + ("" if lib == "none" else f":{lib}") + ("" if atl == "none" else f":atlas-{atl}")


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
    ap.add_argument("--library", choices=LIBRARY_MODES, default="none",
                    help="none: empty library; loo: leave-one-out (never the full library)")
    ap.add_argument("--atlas", default="none",
                    help="none (default) | fixture | real | PATH: which Atlas the reader may consult")
    ap.add_argument("--diff", action="store_true", help="print items that changed verdict since the last run")
    ap.add_argument("--corpus", help="corpus folder (default ~/Workshop/dev/resolver-corpus)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--timing", type=int, nargs="?", const=1, metavar="PASSES",
                    help="print the latency distribution; with PASSES > 1 the tier runs that many "
                         "times and the last (warm) pass is the one reported")
    ap.add_argument("--no-write", action="store_true", help="do not update last_run.json")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--export-g1", action="store_true", help="write corpus/g1.jsonl from the snapshot")
    ap.add_argument("--calibrate", action="store_true",
                    help="fit the confidence thresholds on G1 (5 folds by artist, library none and loo) "
                         "and print the held-out result")
    ap.add_argument("--write-calibration", action="store_true",
                    help="with --calibrate: write app/utils/reader/calibration.json")
    ap.add_argument("--train-target", type=float, help="with --calibrate: the rate the fit aims for (default 0.0025)")
    ap.add_argument("--fit-weights", action="store_true",
                    help="fit the decoder weights on G1+G3 (grouped folds), recalibrate, report held-out "
                         "(app/utils/resolver_fit.py); add --write to update weights_fitted.py and calibration.json")
    ap.add_argument("--folds", type=int, default=10, help="with --fit-weights: grouped folds (default 10)")
    ap.add_argument("--epochs", type=int, default=10, help="with --fit-weights: perceptron epochs")
    ap.add_argument("--out", help="with --fit-weights: folder for report.json and the fitted artefacts")
    ap.add_argument("--write", action="store_true", help="with --fit-weights: write weights_fitted.py and calibration.json")
    ap.add_argument("--weights", choices=("shipped", "fitted", "handset"), default="shipped",
                    help="decoder weights for this run: the shipped reader/weights.py (default), the fitted "
                         "table (reader/weights_fitted.py) or the hand-set table (reader/weights_handset.py)")
    ap.add_argument("--g3-confirmed", choices=("yes", "no"), default="yes",
                    help="with --library loo: treat the other G3 shows' artists as confirmed (default yes)")
    ap.add_argument("--no-fit", action="store_true", help="with --fit-weights: calibration only, no weight fits")
    ap.add_argument("--db", help="snapshot path for --export-g1 (default db/_cowork.db)")
    args = ap.parse_args(argv)

    if args.export_g1:
        meta = export_g1(args.db, args.corpus)
        print(f"g1.jsonl: {meta['n']} items from snapshot dated {meta['snapshot_date']}")
        return 0

    if args.fit_weights:
        from app.utils import resolver_fit
        return resolver_fit.main(args)

    if args.calibrate:
        rep = calibrate(args.corpus, atlas="real", train_target=args.train_target,
                        write=args.write_calibration)
        print(format_calibration(rep))
        if args.write_calibration:
            print("\nwrote app/utils/reader/calibration.json")
        return 0

    set_g3_confirmed(args.g3_confirmed == "yes")
    if args.weights in ("handset", "fitted"):
        from app.utils.reader import decode as _dec
        from app.utils.reader.fit import Model as _Model
        _dec.set_weights(*(_Model.handset() if args.weights == "handset" else _Model.fitted()).tables())
    for _ in range(max(1, args.timing or 1)):
        rep = run_tier(args.tier, "resolve" if args.resolve else "reader",
                       corpus_dir=args.corpus, limit=args.limit, library=args.library, atlas=args.atlas)
    changes = diff_runs(load_last_run(args.corpus).get(_run_key(rep)), rep) if args.diff else None
    if args.json:
        print(json.dumps({k: v for k, v in rep.items() if k != "items"}, indent=2))
    else:
        print(format_report(rep))
        if args.timing:
            print()
            print(format_timing(rep))
    if args.diff:
        print()
        print(format_diff(changes))
    if not args.no_write:
        save_last_run(rep, args.corpus)
    return 0


if __name__ == "__main__":
    sys.exit(main())
