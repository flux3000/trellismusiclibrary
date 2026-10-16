"""Imports Live Music Archive taping policies into assets/reference/artist_policy.csv.

Runs on the Mac, with a network connection, in the project's .venv:

    python3 -m app.reference.build_lma              # fetch, then rewrite the archive.org rows
    python3 -m app.reference.build_lma --offline    # rebuild from the cached download only
    python3 -m app.reference.build_lma --dry-run    # report what would change, write nothing

Every artist collection in the archive's `etree` collection exists only because the
artist gave permission, so each one becomes a Permit All row. The collection's
`rights` field is the archive's own free-text statement of that permission and its
limits; the restrictions it states in a recognisable form become Restrict rows.
Anything restrictive-sounding that no pattern reads goes to a review file, never
into the list: a guessed rule is worse than a missing one.

Dates: a stated month means its first day, a stated year means 1 January, and a
start date is the date as written ("after May 2018" starts 2018-05-01), the same
convention as the DIME rows. policy_date is the latest full date written in the
rights text, else the day the collection went public.

Only the archive.org rows are replaced; etree, DIME and every other source are
left as they are. Each new artist also gets a "name" row in artist_thesaurus.csv,
with a MusicBrainz id when the Atlas has exactly one act by that name.

Archive data carries the archive's non-commercial condition (CONTEXT.md, Archive
downloads); every row it writes says archive.org so it can be removed as a set.
"""
import argparse
import csv
import datetime
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REF = REPO / "assets" / "reference"
POLICY = REF / "artist_policy.csv"
THESAURUS = REF / "artist_thesaurus.csv"
ATLAS = REPO / "assets" / "atlas" / "atlas.sqlite"
DEFAULT_CACHE = Path("~/Workshop/dev/atlas-cache").expanduser()
CACHE_NAME = "lma_collections.json"

SOURCE = "archive.org"
_ROWS = 500
_FIELDS = ("identifier", "title", "rights", "publicdate")

# ---------------------------------------------------------------- date reading
_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DATE_PATTERNS = [
    # 2018-05-12, 2018/05/12
    (re.compile(r"\b(19|20)(\d\d)[-/.](\d{1,2})[-/.](\d{1,2})\b"),
     lambda m: (int(m[1] + m[2]), int(m[3]), int(m[4]), True)),
    # 5/12/2018, 5-12-18
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.]((?:19|20)?\d\d)\b"),
     lambda m: (_year(m[3]), int(m[1]), int(m[2]), True)),
    # May 12, 2018 / May 12 2018
    (re.compile(r"\b" + _MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d\d)\b", re.I),
     lambda m: (int(m[3]), _month(m[1]), int(m[2]), True)),
    # 12 May 2018
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH_RE + r",?\s+((?:19|20)\d\d)\b", re.I),
     lambda m: (int(m[3]), _month(m[2]), int(m[1]), True)),
    # May 2018
    (re.compile(r"\b" + _MONTH_RE + r",?\s+((?:19|20)\d\d)\b", re.I),
     lambda m: (int(m[2]), _month(m[1]), 1, False)),
    # 2018
    (re.compile(r"\b((?:19|20)\d\d)\b"), lambda m: (int(m[1]), 1, 1, False)),
]


def _year(s):
    y = int(s)
    return y if y > 99 else (2000 + y if y <= 30 else 1900 + y)


def _month(s):
    return _MONTHS[s[:3].lower()]


def _iso(y, mo, d):
    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return ""


def first_date(text):
    """The earliest-positioned date in text, as (iso, is_full_date), or ("", False)."""
    best = None
    for rx, conv in _DATE_PATTERNS:
        m = rx.search(text)
        if m and (best is None or m.start() < best[0]):
            y, mo, d, full = conv(m)
            iso = _iso(y, mo, d)
            if iso:
                best = (m.start(), iso, full)
    return (best[1], best[2]) if best else ("", False)


def full_dates(text):
    out = []
    for rx, conv in _DATE_PATTERNS[:4]:
        for m in rx.finditer(text):
            iso = _iso(*conv(m)[:3])
            if iso:
                out.append(iso)
    return out


# ---------------------------------------------------------------- rights reading
_SBD = r"(?:soundboards?|sbds?|board\s+(?:recordings?|tapes?|sources?|feeds?)|matrix(?:es)?)"
_NO_SBD = re.compile(r"\bno\s+(?:more\s+|new\s+)?" + _SBD, re.I)
_SBD_NOT = re.compile(_SBD + r"[^.;]{0,60}?\b(?:not\s+(?:be\s+)?(?:allowed|permitted|acceptable|accepted)|are\s+not\s+ok|prohibited|forbidden)", re.I)
_DONT = re.compile(r"\b(?:do\s+not|don'?t|never)\s+(?:upload|post|share|trade|circulate|seed)\w*\s+(?:any\s+)?" + _SBD, re.I)
_AUD_ONLY = re.compile(r"\b(?:audience|aud)(?:\s+(?:recordings?|tapes?|sources?|uploads?))?\s+only\b|\bonly\s+(?:audience|aud)\b", re.I)
_START = re.compile(r"\b(?:after|since|from|starting|beginning|post|as\s+of|for\s+shows?\s+(?:after|from|since))\b", re.I)
# Words that make an unread sentence worth a human's look.
_CAUTION = re.compile(r"\b(?:no|not|only|except|unless|restrict\w*|remov\w*|prohibit\w*|forbid\w*|stream\w*|request\w*)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.;!?])\s+|\n+")


def parse_rights(text):
    """
    Read one collection's rights text.

    Returns (rules, review): rules is a list of (source, start_date) Restrict rules,
    review is the list of sentences that sound restrictive but matched no pattern.
    A soundboard rule with a start date is read from the same sentence.
    """
    rules, review = [], []
    for sent in _SENTENCE.split(text or ""):
        sent = sent.strip()
        if not sent:
            continue
        hit = (_NO_SBD.search(sent) or _SBD_NOT.search(sent) or _DONT.search(sent)
               or _AUD_ONLY.search(sent))
        if hit:
            start = ""
            tail = sent[hit.start():]
            if _START.search(tail):
                start, _ = first_date(tail[_START.search(tail).start():])
                if not start:
                    review.append(sent)
                    continue
            rule = ("SBD", start)
            if rule not in rules:
                rules.append(rule)
        elif _CAUTION.search(sent) and re.search(_SBD + r"|\baudience\b|\btaping\b|\btrad\w*|\bstream\w*", sent, re.I):
            review.append(sent)
    # One undated SBD rule already covers every dated one.
    if ("SBD", "") in rules:
        rules = [("SBD", "")]
    return rules, review


# ---------------------------------------------------------------- fetching
def fetch_collections(pause=1.0):
    """Every etree artist collection, through advancedsearch, paged."""
    from app.utils.net import SSL_CONTEXT, USER_AGENT   # lazy: parsing needs no app
    docs, page = [], 1
    while True:
        params = [("q", "collection:etree AND mediatype:collection")]
        params += [("fl[]", f) for f in _FIELDS]
        params += [("sort[]", "identifier asc"), ("rows", str(_ROWS)),
                   ("page", str(page)), ("output", "json")]
        url = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as r:
            resp = json.loads(r.read().decode("utf-8", "replace")).get("response") or {}
        batch = resp.get("docs") or []
        docs += batch
        total = int(resp.get("numFound") or 0)
        print(f"  page {page}: {len(docs)} of {total}", file=sys.stderr)
        if not batch or len(docs) >= total:
            return docs
        page += 1
        time.sleep(pause)


def _text(v):
    if isinstance(v, list):
        return " ".join(str(x) for x in v if x)
    return str(v or "")


# ---------------------------------------------------------------- building
def build_rows(docs):
    """(policy rows, thesaurus names, review rows) from collection docs."""
    from app.utils.reader.library import norm_key
    policy, names, review, seen = [], [], [], set()
    for d in docs:
        name = _text(d.get("title")).strip()
        key = norm_key(name)
        if not name or not key or key in seen:
            continue
        seen.add(key)
        rights = _text(d.get("rights"))
        public = _text(d.get("publicdate"))[:10]
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", public):
            public = ""
        dates = full_dates(rights)
        pdate = max(dates) if dates else public
        policy.append([name, "Permit", "All", "", SOURCE, public])
        rules, unread = parse_rights(rights)
        for src, start in rules:
            policy.append([name, "Restrict", src, start, SOURCE, pdate])
        for s in unread:
            review.append([name, _text(d.get("identifier")), s])
        names.append(name)
    return policy, names, review


def _atlas_mbids(names):
    """name -> mbid where the Atlas holds exactly one act under that key."""
    if not ATLAS.is_file():
        return {}
    from app.utils.reader.library import norm_key
    db = sqlite3.connect(f"file:{ATLAS}?mode=ro&immutable=1", uri=True)
    out = {}
    for n in names:
        rows = {r[0] for r in db.execute(
            "select a.mbid from act_name n join act a on a.id = n.act_id where n.key = ?",
            (norm_key(n),)) if r[0]}
        if len(rows) == 1:
            out[n] = rows.pop()
    return out


def _read(path):
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def _write(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _sort_key(r):
    return (r[0].lower().removeprefix("the "), r[4], r[2], r[3])


def apply(policy_new, names, dry_run=False):
    from app.utils.reader.library import norm_key
    ph, prow = _read(POLICY)
    th, trow = _read(THESAURUS)
    kept = [r for r in prow if r[4] != SOURCE]
    # An archive collection named like an artist already listed, or like one of its
    # thesaurus variants, takes that artist's name, so one artist never appears twice.
    canon = {norm_key(r[1]): r[0] for r in trow}
    canon.update({norm_key(r[0]): r[0] for r in kept})
    for r in policy_new:
        r[0] = canon.get(norm_key(r[0]), r[0])
    merged = sorted(kept + policy_new, key=_sort_key)

    have = {r[0] for r in trow}
    missing = sorted({r[0] for r in policy_new} - have)
    mbids = _atlas_mbids(missing)
    trow += [[n, n, "name", mbids.get(n, "")] for n in missing]
    trow.sort(key=lambda r: (r[0].lower().removeprefix("the "), r[2] != "name", r[1].lower()))

    print(f"policy: {len(prow)} -> {len(merged)} rows "
          f"({len(policy_new)} archive.org, {len(kept)} other)", file=sys.stderr)
    print(f"thesaurus: +{len(missing)} name rows, {len(mbids)} with a MusicBrainz id", file=sys.stderr)
    if not dry_run:
        _write(POLICY, ph, merged)
        _write(THESAURUS, th, trow)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache", default=str(DEFAULT_CACHE), help="download cache folder")
    ap.add_argument("--offline", action="store_true", help="use the cached download only")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--review", default="lma_review.csv", help="where unread restrictive sentences go")
    a = ap.parse_args(argv)

    cache = Path(a.cache).expanduser() / CACHE_NAME
    if a.offline:
        docs = json.loads(cache.read_text(encoding="utf-8"))
    else:
        docs = fetch_collections()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(docs), encoding="utf-8")
    print(f"collections: {len(docs)}", file=sys.stderr)

    policy_new, names, review = build_rows(docs)
    restrict = sum(1 for r in policy_new if r[1] == "Restrict")
    print(f"artists: {len(names)}, restrict rules read: {restrict}, sentences to review: {len(review)}",
          file=sys.stderr)
    apply(policy_new, names, dry_run=a.dry_run)
    _write(Path(a.review), ["artist_name", "identifier", "sentence"], review)
    print(f"review file: {a.review}", file=sys.stderr)


if __name__ == "__main__":
    main()
