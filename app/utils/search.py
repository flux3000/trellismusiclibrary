"""
utils/search.py — the V1 search engine (IO-46).

Pure Python: no Flask, no SQLAlchemy, no app context. Callers build a plain
index of dicts (see `build_index`) and hand it to `run_search`. That split is
deliberate — tests/conftest.py holds ONE app context per test and Flask-Login
caches the resolved user on `g`, so anything needing a request context is the
hardest thing in this codebase to test honestly. Keeping the matching logic
pure sidesteps that trap for the part where the bugs actually live.

────────────────────────────────────────────────────────────────────────────
THE RULE  (Ryan, 2026-08-18, re-confirmed at build kickoff)

    "We search only the artist, performer, date, venue, or any combination
     of the above."

Four dimensions. Track titles are NOT searchable, and neither is provenance
free text (`recording.lineage`, `recording.info_file_content`). Both were
approved earlier in that same design session and then explicitly cut — "start
with a basic search that ignores text file and song names." The computed
"Songs — N versions" group depends on title search and therefore does not
exist in V1. Do not add either back without asking; IO-46's Jira description
still promises song identity and is out of date, not a spec.
────────────────────────────────────────────────────────────────────────────

Sizing: this is a ~1,300-row problem. Measured 2026-08-18 against a
deliberately larger 10,696-row set — 15ms to fetch, 15ms to normalise, 0.4ms
to filter. So there is no `search_key` column, no index and no FTS5 table, on
purpose: infix matching is `LIKE '%x%'`, and a leading wildcard cannot use a
B-tree index, so a stored key buys nothing while costing DDL on five tables
plus a write-path maintenance obligation forever. FTS5 is available (SQLite
3.37.2) and becomes the right answer only if the corpus grows ~10x. Keep the
query layer swappable so that stays a substitution rather than a rewrite.
"""

import re
import unicodedata
from datetime import date

# ── Normalisation ──────────────────────────────────────────────────────────

# Both apostrophe characters. The corpus contains straight (Don't Give Your
# Heart To A Rambler, Shuckin' The Corn) and curly (Bear's Ampex cassette).
_APOSTROPHES = ("'", "’")

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE    = re.compile(r"\s+")


def norm(s):
    """
    Fold a name or query fragment to a comparison key.

    Apostrophes are DELETED, every other punctuation mark becomes a SPACE.
    Getting that backwards silently breaks a large fraction of this corpus:
    map the apostrophe to a space and "Don't" becomes "don t", so a user
    typing "dont give your heart" matches nothing at all. A benchmark caught
    it on the first run during design; it is the single most load-bearing
    line in this module.

    Diacritics are folded too, so "esbjorn" finds "Esbjörn". Unicode form is
    settled on the way through (NFD to strip the marks, back to NFC), which
    also covers the case a name arrives decomposed from macOS Finder —
    filename-vs-database normalisation mismatch has already broken a whole
    folder-to-grade join in this project once (see CONTEXT, "Traps").

    Deliberately NOT consolidated with `checksums._norm_name` or
    `quality_store._canon_path`, despite the design note suggesting it. Those
    two build KEYS FOR JOINS, where folding two distinct names together is a
    correctness bug. This one builds keys for FUZZY HUMAN MATCHING, where
    folding them together is the entire point. Same shape, opposite contract.
    """
    if not s:
        return ""
    # NFD then drop the combining marks: "Esbjörn" folds to "esbjorn" so a
    # collector typing plain ASCII finds Esbjörn Svensson (51 recordings, the
    # single densest act in the library). The corpus is full of accented
    # names — Lucía, Arènes et Jardins de Cimiez, Pinède Gould — and typing
    # them correctly requires knowing the diacritic before you can search for
    # it, which is backwards. Collisions this could create ("Bohse"/"Böhse")
    # are theoretical here and would merely widen a result, never hide one.
    s = unicodedata.normalize("NFD", str(s)).lower()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = unicodedata.normalize("NFC", s)
    for ch in _APOSTROPHES:
        s = s.replace(ch, "")
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def keys(*values):
    """Normalise several raw strings into a de-duplicated list of match keys."""
    out = []
    for v in values:
        k = norm(v)
        if k and k not in out:
            out.append(k)
    return out


# ── Date parsing ───────────────────────────────────────────────────────────
#
# Dates are parsed from the RAW token, before normalisation — norm() strips
# the separators, so "1983-04-12" would arrive here as "1983 04 12" and no
# longer look like a date at all. Order matters.

_ISO_RE   = re.compile(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$")
_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")
_YEAR_RE  = re.compile(r"^(\d{4})$")

# A bare 4-digit number in this range is read as a year. Shows in the library
# span 1940–2026; the wider window costs nothing and avoids re-tuning.
_YEAR_MIN, _YEAR_MAX = 1900, 2099


def _expand_two_digit_year(yy, today_year=None):
    """
    Expand the 2-digit year inside a slashed date (4/12/83 → 1983).

    Pivot on the current 2-digit year rather than a hardcoded constant: the
    corpus runs 1940–2026, so "83" is unambiguously 1983 and "12" is 2012,
    and the boundary moves correctly as time passes.

    Note this applies ONLY inside an unambiguous slashed date. A BARE
    two-digit token is deliberately NOT a date — Ryan, 2026-08-18: with shows
    from 1940 to 2026 a lone "26" is genuinely ambiguous, so it stays text and
    we never silently guess.
    """
    ty = today_year if today_year is not None else date.today().year
    pivot = ty % 100
    return (2000 + yy) if yy <= pivot else (1900 + yy)


def parse_date_token(tok, today_year=None):
    """
    Try to read one raw token as a date.

    Returns (year, month, day) with None for unspecified components, or None
    if the token is not a date. Accepted forms:

        1983          → (1983, None, None)
        1983-04       → (1983, 4, None)
        1983-04-12    → (1983, 4, 12)
        4/12/83       → (1983, 4, 12)
        4/12/1983     → (1983, 4, 12)

    Deliberately NOT accepted: a bare 2-digit year, and a bare "4/12" with no
    year. Both are ambiguous and both fall through to being matched as text.
    """
    tok = (tok or "").strip()
    if not tok:
        return None

    m = _ISO_RE.match(tok)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        d = int(m.group(3)) if m.group(3) else None
        if _valid_ymd(y, mo, d):
            return (y, mo, d)
        return None

    m = _SLASH_RE.match(tok)
    if m:
        mo, d, raw_y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(raw_y) if len(raw_y) == 4 else _expand_two_digit_year(int(raw_y), today_year)
        if _valid_ymd(y, mo, d):
            return (y, mo, d)
        return None

    m = _YEAR_RE.match(tok)
    if m:
        y = int(m.group(1))
        if _YEAR_MIN <= y <= _YEAR_MAX:
            return (y, None, None)
        return None

    return None


def _valid_ymd(y, mo, d):
    if not (_YEAR_MIN <= y <= _YEAR_MAX):
        return False
    if mo is not None and not (1 <= mo <= 12):
        return False
    if d is not None and not (1 <= d <= 31):
        return False
    return True


def date_matches(term, y, mo, d):
    """
    Does a show dated (y, mo, d) satisfy this date term?

    Only the components the user actually typed are compared, so "1983"
    matches every show that year and "1983-04-12" matches exactly one day.
    A show with no year recorded matches no date term.
    """
    ty, tmo, td = term
    if y is None or y != ty:
        return False
    if tmo is not None and mo != tmo:
        return False
    if td is not None and d != td:
        return False
    return True


# ── Query parsing ──────────────────────────────────────────────────────────

class Query:
    """A parsed query: some text terms, some date terms, ANDed together."""

    __slots__ = ("raw", "text_terms", "date_terms")

    def __init__(self, raw, text_terms, date_terms):
        self.raw = raw
        self.text_terms = text_terms
        self.date_terms = date_terms

    def __bool__(self):
        return bool(self.text_terms or self.date_terms)

    def __repr__(self):                                   # pragma: no cover
        return f"<Query text={self.text_terms!r} dates={self.date_terms!r}>"


def parse_query(q, today_year=None):
    """
    Split a raw query into ANDed text and date terms.

    Multi-term is AND by design (Ryan): typing more must NARROW rather than
    break, so "hot rize 1983" means the act AND the year. There is no
    field-prefix grammar ("performer:"), no quoting and no negation — the
    invited cohort will never use it, and every one of those is a way for a
    query to silently mean something the user didn't intend.

    A token is tested as a date first (against the RAW token, before norm()
    eats the separators); anything else is normalised into a text term.
    """
    raw = (q or "").strip()
    text_terms, date_terms = [], []
    for tok in raw.split():
        dt = parse_date_token(tok, today_year=today_year)
        if dt is not None:
            if dt not in date_terms:
                date_terms.append(dt)
            continue
        t = norm(tok)
        if t and t not in text_terms:
            text_terms.append(t)
    return Query(raw, text_terms, date_terms)


# ── Match scoring ──────────────────────────────────────────────────────────
#
# Ranking is match strength first, then listening quality as a tiebreak among
# equally good matches (Ryan, 2026-08-18). Quality is the only dense signal —
# it covers 100% of recordings, where hand grades cover ~22% and favourites
# cover two. CONTEXT's caveat that the quality model is "far less reliable at
# the bad end than the good end" is about HIDING things; ordering equally
# relevant hits best-sounding-first hides nothing.

EXACT      = 100   # the whole key is the term
PREFIX     = 80    # the key starts with the term
WORD_START = 60    # the term starts a word inside the key
INFIX      = 40    # the term appears mid-word


def score_text_term(term, candidate_keys):
    """
    Best score for one text term across a row's match keys, 0 for no match.

    Match strength is graded rather than boolean so "evans" ranks Bill Evans
    above a venue called "Evanston Auditorium" without either being excluded.
    """
    best = 0
    for k in candidate_keys:
        if not k:
            continue
        if k == term:
            return EXACT                      # cannot be beaten; stop early
        if k.startswith(term):
            s = PREFIX
        elif (" " + term) in k:
            s = WORD_START
        elif term in k:
            s = INFIX
        else:
            continue
        if s > best:
            best = s
    return best


def score_row(query, candidate_keys, ymd=None):
    """
    Score one row against every term, or return None if it fails any of them.

    AND semantics live here: a row must satisfy EVERY term to survive, and
    the returned score is the mean strength across them. `ymd` is the show's
    (year, month, day); rows with no date (an act, a venue) are scored on
    text terms only and are simply not offered when the query is date-only.
    """
    scores = []

    for term in query.text_terms:
        s = score_text_term(term, candidate_keys)
        if not s:
            return None
        scores.append(s)

    if query.date_terms:
        if ymd is None:
            return None
        y, mo, d = ymd
        for term in query.date_terms:
            if not date_matches(term, y, mo, d):
                return None
            scores.append(EXACT)

    if not scores:
        return None
    return sum(scores) / len(scores)


# ── Index construction ─────────────────────────────────────────────────────

def build_index(performers, artists, venues, recordings):
    """
    Precompute match keys for every searchable row.

    Each argument is a list of plain dicts — the API layer builds them from
    column-level queries, never ORM objects, so no relationship is lazily
    walked per row. Expected shapes:

      performer  {id, name, sort_name}
      artist     {id, name, sort_name, performer_ids: [int]}
      venue      {id, name, city, state, country}
      recording  {id, performance_id, performer_id, performer_name,
                  performer_sort_name, artist_names: [str], venue_name,
                  city, state, country, year, month, day, source,
                  listening_quality}

    Normalisation happens once here rather than once per query term, which is
    what keeps the measured cost at ~15ms for the whole corpus.
    """
    idx = {"performers": [], "artists": [], "venues": [], "recordings": []}

    for p in performers:
        idx["performers"].append({**p, "_keys": keys(p.get("name"), p.get("sort_name"))})

    for a in artists:
        idx["artists"].append({**a, "_keys": keys(a.get("name"), a.get("sort_name"))})

    for v in venues:
        idx["venues"].append({
            **v,
            "_keys": keys(v.get("name"), v.get("city"), v.get("state"), v.get("country")),
        })

    for r in recordings:
        # A recording's searchable text is the union of its three text
        # dimensions: the act, its members (artist reaches shows through
        # membership, NOT performance_personnel — Ryan, 2026-08-18, matching
        # the precedent already set for peer artist visibility), and the
        # venue including its geography.
        #
        # Geography comes from the VENUE, not the performance. Measured
        # 2026-08-18: only 13 of 552 performances carry a `city` of their
        # own, so performance.city is effectively empty and reading it as
        # the primary source would lose the dimension. The 10 shows with no
        # venue at all are unreachable by geography — accepted, not a bug.
        idx["recordings"].append({
            **r,
            "_keys": keys(
                r.get("performer_name"), r.get("performer_sort_name"),
                *(r.get("artist_names") or []),
                r.get("venue_name"), r.get("city"), r.get("state"), r.get("country"),
            ),
        })

    return idx


# ── The search itself ──────────────────────────────────────────────────────

GROUP_LABELS = {
    "performers": "Performers",
    "recordings": "Recordings",
    "venues":     "Venues",
    "artists":    "Artists",
}

# Fixed group order rather than reordering by best match. A dropdown whose
# groups reshuffle between keystrokes is impossible to aim at — the user
# starts moving toward a row that has already moved.
GROUP_ORDER = ("performers", "recordings", "venues", "artists")


def _sort_key_entity(entry):
    """Entities: strongest match first, then alphabetical for a stable order."""
    return (-entry["score"], entry["row"].get("_keys", [""])[0] if entry["row"].get("_keys") else "")


def _sort_key_recording(entry):
    """
    Shows: strongest match, then best-sounding, then most recent show.

    listening_quality is None only if a recording has never been analysed
    (currently zero of them, but the column is nullable); -1 sorts those last
    rather than crashing the comparison.
    """
    r = entry["row"]
    lq = r.get("listening_quality")
    return (
        -entry["score"],
        -(lq if lq is not None else -1),
        -(r.get("year") or 0), -(r.get("month") or 0), -(r.get("day") or 0),
    )


def run_search(index, q, today_year=None):
    """
    Run a query against a built index.

    Returns {"query", "text_terms", "date_terms", "groups"} where each group
    is {"label", "total", "items": [{"row", "score"}, ...]} fully ranked and
    unsliced — paging and payload shaping belong to the API layer, so the
    dropdown and the results page can slice the same result differently.

    Entity groups (Performers, Venues, Artists) are matched on TEXT terms only; a
    date term does not exclude them, it simply isn't something an act can
    satisfy. So "hot rize 1983" still offers the act itself alongside that
    year's shows, which is what a user reaching for a band wants. The
    corollary: a date-ONLY query returns Shows and nothing else, because
    matching every act in the library against no text is not a search result.
    """
    query = parse_query(q, today_year=today_year)
    groups = {}

    for key in GROUP_ORDER:
        groups[key] = {"label": GROUP_LABELS[key], "total": 0, "items": []}

    if not query:
        return {"query": query.raw, "text_terms": [], "date_terms": [], "groups": groups}

    # Entities — text terms only.
    if query.text_terms:
        entity_query = Query(query.raw, query.text_terms, [])
        for key in ("performers", "venues", "artists"):
            hits = []
            for row in index.get(key, []):
                s = score_row(entity_query, row["_keys"])
                if s is not None:
                    hits.append({"row": row, "score": s})
            hits.sort(key=_sort_key_entity)
            groups[key]["items"] = hits
            groups[key]["total"] = len(hits)

    # Shows — every term, text and date, across the union of the three text
    # dimensions plus the show's own date.
    hits = []
    for row in index.get("recordings", []):
        ymd = (row.get("year"), row.get("month"), row.get("day"))
        s = score_row(query, row["_keys"], ymd=ymd)
        if s is not None:
            hits.append({"row": row, "score": s})
    hits.sort(key=_sort_key_recording)
    groups["recordings"]["items"] = hits
    groups["recordings"]["total"] = len(hits)

    return {
        "query":      query.raw,
        "text_terms": list(query.text_terms),
        "date_terms": [list(t) for t in query.date_terms],
        "groups":     groups,
    }
