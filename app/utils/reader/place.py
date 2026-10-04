"""
Place peeler (Resolver v2, chunk 2, 2026-10-03).

Peels a place off the RIGHT end of a line or segment run, in this order:

  1. country  (aliases: U.K., UK, USA, U.S.A., United States, ...)
  2. region   (US states and DC; Canadian provinces; Australian states; the UK
               nations; German Laender)
  3. city     (checked against GeoNames cities of that region or country)

What is left of the clause to the left of the city is the venue candidate
(`left`); other clauses of the line (split on " @ ", " - ", ";", " / ") are in
`others`. Abbreviations are read as written, before any casing: "ON" is
Ontario, "on" is a word unless the city to its left is in Ontario.

Region and country data: geonamescache ships US states, countries and cities
with admin1 codes, but no admin1 names. The Canadian, Australian, UK and German
region tables below are explicit; their admin1 codes are the GeoNames ones
(CA "08" = Ontario, DE "02" = Bavaria, GB "ENG", ...).

Output conventions (unchanged from ingest.py):
  country  "US" for US states, "UK" for the UK and its nations, otherwise the
           gazetteer name ("Canada", "France", "Japan")
  region   US state code ("VA"); for other countries the region name as
           written canonically ("Ontario", "Bavaria", "England")
  state    the code for a US state, Canadian province or Australian state
           ("ON", "WA"); empty for every other country
"""
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

import geonamescache as _geonamescache

from .dates import find_dates
from .segment import segment_line

# ── Reference tables ──────────────────────────────────────────────────────────

_gc = _geonamescache.GeonamesCache()
_US = {code: v["name"] for code, v in _gc.get_us_states().items()}   # includes DC
_COUNTRIES = _gc.get_countries()                                      # iso -> record

_CA = {"AB": ("Alberta", "01"), "BC": ("British Columbia", "02"), "MB": ("Manitoba", "03"),
       "NB": ("New Brunswick", "04"), "NL": ("Newfoundland and Labrador", "05"),
       "NS": ("Nova Scotia", "07"), "ON": ("Ontario", "08"),
       "PE": ("Prince Edward Island", "09"), "QC": ("Quebec", "10"),
       "SK": ("Saskatchewan", "11"), "YT": ("Yukon", "12"),
       "NT": ("Northwest Territories", "13"), "NU": ("Nunavut", "14")}
_CA_EXTRA = {"québec": "QC", "newfoundland": "NL", "yukon territory": "YT",
             "pei": "PE"}
_CA_FREE = {"ON", "QC", "BC", "AB", "MB", "SK", "NB"}   # unique enough to need no city check

_AU = {"ACT": ("Australian Capital Territory", "01"), "NSW": ("New South Wales", "02"),
       "NT": ("Northern Territory", "03"), "QLD": ("Queensland", "04"),
       "SA": ("South Australia", "05"), "TAS": ("Tasmania", "06"),
       "VIC": ("Victoria", "07"), "WA": ("Western Australia", "08")}

_GB = {"ENG": ("England", "ENG"), "SCT": ("Scotland", "SCT"), "WLS": ("Wales", "WLS"),
       "NIR": ("Northern Ireland", "NIR")}

# German Laender: admin1 code -> (name, alternative spellings)
_DE = {"01": ("Baden-Württemberg", ["baden wuerttemberg", "baden württemberg"]),
       "02": ("Bavaria", ["bayern"]),
       "03": ("Bremen", []), "04": ("Hamburg", []),
       "05": ("Hesse", ["hessen"]),
       "06": ("Lower Saxony", ["niedersachsen"]),
       "07": ("North Rhine-Westphalia", ["nordrhein-westfalen", "nordrhein westfalen"]),
       "08": ("Rhineland-Palatinate", ["rheinland-pfalz", "rheinland pfalz"]),
       "09": ("Saarland", []), "10": ("Schleswig-Holstein", []),
       "11": ("Brandenburg", []), "12": ("Mecklenburg-Vorpommern", []),
       "13": ("Saxony", ["sachsen"]),
       "14": ("Saxony-Anhalt", ["sachsen-anhalt", "sachsen anhalt"]),
       "15": ("Thuringia", ["thüringen", "thuringen"]),
       "16": ("Berlin", [])}

# Region names that are also city names or country names: read as a region
# only when a city text stands to their left ("Buffalo, New York").
_AMBIGUOUS_NAMES = {"washington", "new york", "georgia", "victoria", "berlin",
                    "hamburg", "bremen", "luxembourg"}

_CITY_ABBREVS = {"sf": ("San Francisco", "CA"), "la": ("Los Angeles", "CA"),
                 "philly": ("Philadelphia", "PA"), "nola": ("New Orleans", "LA")}
_DC_ALIASES = {"washdc", "washingtondc", "wdc"}   # a whole segment that names the city AND the region

_ISO3 = {rec["iso3"]: iso for iso, rec in _COUNTRIES.items()}
_COUNTRY_SHORT = {"US": "US", "GB": "UK"}         # display names that differ from the gazetteer


def _key(s):
    return re.sub(r"[.()\s]", "", s).lower()


@lru_cache(maxsize=8192)
def _fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _country_display(iso):
    name = _COUNTRIES[iso]["name"]
    return _COUNTRY_SHORT.get(iso) or (name[4:] if name.startswith("The ") else name)


@lru_cache(maxsize=1)
def _country_keys():
    keys = {}
    for iso, rec in _COUNTRIES.items():
        k = _key(_fold(rec["name"]))
        keys[k] = iso
        if k.startswith("the"):
            keys[k[3:]] = iso          # "The Netherlands" is written "Netherlands"
    for k in ("us", "usa", "unitedstates", "unitedstatesofamerica", "america", "theus", "theusa"):
        keys[k] = "US"
    for k in ("uk", "unitedkingdom", "greatbritain", "britain", "gb"):
        keys[k] = "GB"
    keys.update({"holland": "NL", "thenetherlands": "NL", "deutschland": "DE",
                 "czechrepublic": "CZ", "korea": "KR"})
    keys.pop("gb", None)       # "GB" is too often gigabytes; ISO2 path validates a city instead
    return keys


_COUNTRY_SUFFIX_OK = {"us", "usa", "uk", "unitedstates", "unitedstatesofamerica", "unitedkingdom"}
_COUNTRY_AMBIGUOUS = {"georgia"}   # also a US state: country only when a Georgian city is to its left


def _norm_city(s):
    s = _fold(s).lower().replace("’", "'")
    s = re.sub(r"[^a-z0-9' ]+", " ", s).replace("'", "")
    words = [{"st": "saint", "ft": "fort", "mt": "mount"}.get(w, w) for w in s.split()]
    out = " ".join(words)
    return {"nyc": "new york", "new york city": "new york", "sf": "san francisco", "philly": "philadelphia",
            "nola": "new orleans"}.get(out, out)


@lru_cache(maxsize=1)
def _city_index():
    by_region, by_country, any_city = {}, {}, {}
    for rec in _gc.get_cities().values():
        cc, a1 = rec["countrycode"], rec["admin1code"]
        names = {rec["name"]} | {n for n in rec.get("alternatenames", ()) if n and n[0].isascii() and n[0].isalpha()}
        for n in names:
            k = _norm_city(n)
            if not k:
                continue
            by_region.setdefault((cc, a1), set()).add(k)
            by_country.setdefault(cc, set()).add(k)
            if n == rec["name"] or len(names) < 3:
                prev = any_city.get(k)
                if prev is None or rec["population"] > prev[2]:
                    any_city[k] = (cc, a1, rec["population"])
    return by_region, by_country, any_city


def _city_in(text, cc, admin1=None):
    by_region, by_country, _ = _city_index()
    k = _norm_city(text)
    if not k:
        return False
    if admin1:
        return k in by_region.get((cc, admin1), ())
    return k in by_country.get(cc, ())


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class PlaceResult:
    city: str = ""
    region: str = ""          # US code, or region name for other countries
    region_code: str = ""     # state/province code for US, CA and AU ("ON", "WA"); "" elsewhere
    country: str = ""         # "US", "UK", gazetteer name, or "" when not stated/derivable
    left: str = ""            # text left of the place in its clause (venue candidate)
    others: list = field(default_factory=list)    # other clauses: {text, span, sep, side}
    evidence: list = field(default_factory=list)  # {text, span, kind}
    span: tuple = (0, 0)      # consumed span in the input text
    confidence: str = ""      # "high" | "medium" | "low" | ""
    city_validated: bool = False

    @property
    def recognised(self):
        return bool(self.region or self.country or self.city)

    @property
    def has_region_or_country(self):
        return bool(self.region or self.country)

    @property
    def state(self):
        """State as ingest stores it: the code for a US state, Canadian province or
        Australian state ("ON", "NSW"). Other countries leave it empty (decision 2026-10-03)."""
        return self.region_code

    def venue_candidate(self):
        """The text that should be considered the venue: what stands left of the
        place in its clause, else the clause attached by " @ " or a dash."""
        if self.left:
            return self.left
        for o in self.others:
            if o["side"] == "right" and "@" in o["sep"]:
                return o["text"]
        for o in reversed(self.others):
            if o["side"] == "left":
                return o["text"]
        for o in self.others:
            if o["side"] == "right":
                return o["text"]
        return ""


# ── Matching pieces ───────────────────────────────────────────────────────────

@dataclass
class _Cand:
    kind: str            # "region" | "country"
    cc: str
    code: str            # region code or ISO2
    display: str         # region display (US: code), or country display
    admin1: str = ""
    code_form: bool = False
    free: bool = False   # a code that needs no city validation
    ambiguous: bool = False
    builtin_city: str = ""


@lru_cache(maxsize=8192)
def _region_cands(tok):
    """Every reading of `tok` as a region (or an ISO2 country). `tok` is as written."""
    letters = re.sub(r"[.\s]", "", tok)
    key = letters.lower()
    out = []
    code_form = bool(re.fullmatch(r"[A-Za-z]{2,3}", letters))
    up = letters.upper()
    if key in ("nyc", "newyorkcity") and tok[:1].isupper():
        out.append(_Cand("region", "US", "NY", "NY", "NY", builtin_city="New York"))
    if key in _DC_ALIASES:
        out.append(_Cand("region", "US", "DC", "DC", "DC", builtin_city="Washington"))
    if tok[:1].isupper() and ((key in _CITY_ABBREVS and key != "la") or (key == "la" and "." in tok)):
        city, st = _CITY_ABBREVS[key]
        out.append(_Cand("region", "US", st, st, st, builtin_city=city))
    if code_form and up in _US:
        out.append(_Cand("region", "US", up, up, up, code_form=True, free=True,
                         builtin_city="Washington" if up == "DC" else ""))
    name_key = re.sub(r"\s+", " ", tok.strip().lower().replace(".", ""))
    for code, name in _US.items():
        if name.lower() == name_key and code != "DC":
            out.append(_Cand("region", "US", code, code, code,
                             ambiguous=name_key in _AMBIGUOUS_NAMES))
    abbrev = _US_ABBREV.get(key)
    if abbrev:
        out.append(_Cand("region", "US", abbrev, abbrev, abbrev))
    for code, (name, a1) in _CA.items():
        if (code_form and up == code) or name.lower() == name_key or _CA_EXTRA.get(name_key) == code:
            is_code = code_form and up == code
            out.append(_Cand("region", "CA", code, name, a1, code_form=is_code,
                             free=is_code and code in _CA_FREE))
    for code, (name, a1) in _AU.items():
        if (code_form and up == code) or name.lower() == name_key:
            out.append(_Cand("region", "AU", code, name, a1, code_form=code_form and up == code))
    for code, (name, a1) in _GB.items():
        if name.lower() == name_key:
            out.append(_Cand("region", "GB", code, name, a1))
    for a1, (name, alts) in _DE.items():
        if _fold(name).lower() == _fold(name_key) or name_key in alts:
            out.append(_Cand("region", "DE", a1, name, a1, ambiguous=name.lower() in _AMBIGUOUS_NAMES))
    if code_form and len(up) == 2 and up in _COUNTRIES:
        out.append(_Cand("country", up, up, _country_display(up), code_form=True))
    if code_form and len(up) == 3 and up in _ISO3:
        out.append(_Cand("country", _ISO3[up], _ISO3[up], _country_display(_ISO3[up]), code_form=True))
    return tuple(out)


_US_ABBREV = {"calif": "CA", "penn": "PA", "penna": "PA", "mass": "MA", "tenn": "TN",
              "fla": "FL", "conn": "CT", "mich": "MI", "minn": "MN", "colo": "CO",
              "ariz": "AZ", "okla": "OK", "wisc": "WI", "wis": "WI"}


def _cand_city_ok(city_text, c):
    if not city_text:
        return False
    if c.kind == "country":
        return _city_in(city_text, c.cc)
    return _city_in(city_text, c.cc, c.admin1)


# words that attach to the city after them ("East Hempstead", "Port Chester")
_CITY_MODIFIERS = {"east", "west", "north", "south", "new", "old", "upper", "lower", "great",
                   "little", "port", "fort", "ft", "mount", "mt", "saint", "st", "san", "santa",
                   "los", "las", "el", "la", "le", "de", "del", "al", "ben", "bad", "glen"}
_WORD_CODES = {"Me", "Hi", "Or", "In", "Oh", "Ok", "Id", "Ma", "Pa", "La", "Al", "De", "As", "Is", "It", "On"}


def _pick_region(tok, known_cc, city_text, suffix=False):
    """-> (candidate or None, city_validated). Abbreviations are judged as written."""
    letters = re.sub(r"[.\s]", "", tok)
    upper = letters.isupper()
    cands = _region_cands(tok)
    if known_cc:
        cands = [c for c in cands if c.kind == "region" and c.cc == known_cc]
    ok = [c for c in cands if _cand_city_ok(city_text, c)]
    if ok:
        return ok[0], True
    for c in cands:
        if c.kind == "country":
            continue
        if c.code_form:
            capitalised = letters[:1].isupper() and letters[1:].islower() and letters not in _WORD_CODES
            if not (c.free and (upper or capitalised) or (c.cc == "US" and capitalised)):
                continue
        elif not tok[:1].isupper():
            continue
        if c.ambiguous and (suffix or not city_text):
            continue
        return c, False
    return None, False


def _match_country(tok, city_text):
    letters = re.sub(r"[.()\s]", "", tok)
    key = letters.lower()
    iso = _country_keys().get(_fold(key))
    if not iso:
        return None
    if key in _COUNTRY_AMBIGUOUS and not _city_in(city_text, iso):
        return None
    if len(key) <= 3 and not letters.isupper() and not _city_in(city_text, iso):
        return None
    return iso


_NICE_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")


def _nice(s):
    s = s.strip()
    if s.isupper() or s.islower():
        return _NICE_WORD.sub(lambda m: m.group(0).capitalize(), s)
    return s


def _city_text_ok(t):
    return bool(t) and not re.search(r"\d", t) and len(t.split()) <= 5


_TRAIL_SEP = re.compile(r"[\s,;@/\-–—]+$")
_WEEKDAY_BEFORE = re.compile(
    r"\b(?:Mon|Tues?|Wed(?:nes)?|Thu(?:rs?)?|Fri|Sat(?:ur)?|Sun)(?:day)?\b\.?[\s,]*$", re.I)


def _words(text):
    return [(m.group(), m.start(), m.end()) for m in re.finditer(r"\S+", text)]


# ── The peeler ────────────────────────────────────────────────────────────────

def _peel_clause(segs, work, city_only):
    """Peel one clause (a run of comma-joined segments). -> PlaceResult or None."""
    if not segs:
        return None
    i = len(segs) - 1
    cur = [segs[i].text, segs[i].start, segs[i].end]      # working right-most piece
    ev = []
    country_cc = None
    country_span = None
    country_suffix = False
    region = None
    city = None
    city_span = None
    validated = False

    def prev_text():
        return segs[i - 1].text if i > 0 else ""

    def step_left():
        nonlocal i
        i -= 1
        if i >= 0:
            cur[:] = [segs[i].text, segs[i].start, segs[i].end]
        else:
            cur[:] = ["", segs[0].start, segs[0].start]

    # 1. country: the whole piece, then a trailing word run ("Paris France")
    iso = _match_country(cur[0], prev_text())
    if iso:
        country_cc = iso
        ev.append({"text": cur[0], "span": (cur[1], cur[2]), "kind": "country"})
        country_span = (cur[1], cur[2])
        step_left()
    else:
        ws = _words(cur[0])
        for k in (3, 2, 1):
            if len(ws) > k:
                tail = cur[0][ws[-k][1]:]
                head = cur[0][:ws[-k][1]].strip()
                iso = _match_country(tail, head)
                if iso and not (tail.startswith("(") or _city_in(head, iso)
                                or re.sub(r"[.()\s]", "", tail).lower() in _COUNTRY_SUFFIX_OK):
                    iso = None
                if iso:
                    country_cc = iso
                    country_suffix = True
                    ev.append({"text": tail, "span": (cur[1] + ws[-k][1], cur[2]), "kind": "country"})
                    country_span = (cur[1] + ws[-k][1], cur[2])
                    cur[:] = [head, cur[1], cur[1] + len(head)]
                    break

    # 2. region: whole piece (city on the previous segment), then a trailing run
    cand = None
    if i >= 0 and cur[0]:
        cand, validated = _pick_region(cur[0], country_cc, prev_text())
        if cand is not None:
            ev.append({"text": cur[0], "span": (cur[1], cur[2]), "kind": "region"})
            region_span = (cur[1], cur[2])
            step_left()
        else:
            ws = _words(cur[0])
            for k in (3, 2, 1):
                if len(ws) > k:
                    tail = cur[0][ws[-k][1]:]
                    head = cur[0][:ws[-k][1]].strip()
                    if not head or not (head[-1].isalpha() or head[-1] in ".'"):
                        continue
                    c2, v2 = _pick_region(tail, country_cc, head, suffix=True)
                    if c2 is not None:
                        cand, validated = c2, v2
                        ev.append({"text": tail, "span": (cur[1] + ws[-k][1], cur[2]), "kind": "region"})
                        region_span = (cur[1] + ws[-k][1], cur[2])
                        cur[:] = [head, cur[1], cur[1] + len(head)]
                        break

    if cand is not None and cand.kind == "country":
        # an ISO2 country code ("Paris, FR"), accepted only because the city validated
        country_cc = cand.cc
        cand_region = None
        validated = True
        ev[-1]["kind"] = "country"
    else:
        cand_region = cand

    if cand_region is not None:
        country_cc = country_cc or cand_region.cc
        region = cand_region
    # 3. city
    if cand is not None or country_cc:
        if cand is not None and cand.builtin_city:
            city = cand.builtin_city
            city_span = region_span
            validated = True
            # swallow a preceding "Washington"/"Wash." that spells the same city
            if cur[0] and _norm_city(cur[0]) in ("washington", "wash"):
                city_span = (cur[1], region_span[1])
                step_left()
        elif cur[0]:
            text0, base = cur[0], cur[1]
            admin1 = region.admin1 if region else None
            ccx = region.cc if region else country_cc
            whole_ok = _city_text_ok(text0)
            if whole_ok and ccx and _city_in(text0, ccx, admin1):
                city, city_span, validated = text0, (base, cur[2]), True
            if not city and ccx:
                # "Fillmore East San Francisco": the longest trailing run of words that is a city
                ws = _words(text0)
                for k in (3, 2, 1):
                    if (len(ws) > k and ws[-k - 1][0].lower().strip(".") not in _CITY_MODIFIERS
                            and _city_in(text0[ws[-k][1]:], ccx, admin1)):
                        city = text0[ws[-k][1]:]
                        city_span = (base + ws[-k][1], cur[2])
                        validated = True
                        break
            if not city:
                # a column gap ("Festival  Chattanooga") marks where the city starts
                gap = list(re.finditer(r"\s{2,}", text0))
                if gap and gap[-1].end() < len(text0) and _city_text_ok(text0[gap[-1].end():]):
                    city = text0[gap[-1].end():]
                    city_span = (base + gap[-1].end(), cur[2])
                elif whole_ok:
                    city, city_span = text0, (base, cur[2])
            step_left()
        if city:
            ev.append({"text": city, "span": city_span, "kind": "city"})
    elif city_only:
        t = cur[0]
        if t and t[0].isupper() and len(t.split()) <= 3 and _city_text_ok(t):
            hit = _city_index()[2].get(_norm_city(t))
            if hit and hit[2] >= 50000:
                city, city_span = t, (cur[1], cur[2])
                ev.append({"text": t, "span": city_span, "kind": "city"})
                step_left()
                country_cc = None
                res = PlaceResult(city=_nice(city), confidence="low")
                res.span = city_span
                res.left = work[segs[0].start:city_span[0]]
                res.left = _TRAIL_SEP.sub("", res.left).strip()
                res.evidence = ev
                return res
        return None
    else:
        return None

    if not (region or country_cc or city):
        return None
    if country_suffix and region is None and not validated:
        return None          # a stray "(USA)" after a title is not a place

    r = PlaceResult()
    if region is not None:
        r.region = region.code if region.cc == "US" else region.display
        r.region_code = region.code if region.cc in ("US", "CA", "AU") else ""
    if country_cc:
        r.country = _country_display(country_cc)
    elif region is not None:
        r.country = _country_display(region.cc)
    r.city = _nice(city) if city else ""
    if r.city and _norm_city(r.city) == "new york":
        r.city = "New York"            # NYC, New York City
    elif r.city and _norm_city(r.city) in ("san francisco", "philadelphia", "new orleans") and \
            r.city.lower().replace(".", "") in ("sf", "philly", "nola"):
        r.city = {"sf": "San Francisco", "philly": "Philadelphia", "nola": "New Orleans"}[r.city.lower().replace(".", "")]
    r.city_validated = bool(validated and city)
    spans = [e["span"] for e in ev]
    start = min(s for s, _ in spans)
    end = max(e for _, e in spans)
    r.span = (start, end)
    r.left = _TRAIL_SEP.sub("", work[segs[0].start:start]).strip()
    r.evidence = ev
    if city and r.city_validated:
        r.confidence = "high"
    else:
        r.confidence = "medium"
    return r


def peel(text, city_only=True):
    """Cached; the returned PlaceResult is shared, treat it as read-only."""
    return _peel_cached(text or "", bool(city_only))


@lru_cache(maxsize=4096)
def _peel_cached(text, city_only):
    """Peel the place off the right of `text` (a line or a segment run).

    Date mentions are blanked first so "JULY 11,1981" is never read as part of
    the place. Clauses (split on " @ ", " - ", ";", " / ") are tried right to
    left; the first that yields a region or country wins. Only when none does,
    and `city_only` is set, a bare gazetteer city at the right of a clause is
    accepted (low confidence, country left empty).
    """
    text = text or ""
    work = text
    mentions = find_dates(text)
    if mentions:
        for m in mentions:
            a, b = m.span
            wd = _WEEKDAY_BEFORE.search(text[:a])      # "Friday, March 22, 2002"
            if wd:
                a = wd.start()
            work = work[:a] + " " * (b - a) + work[b:]
    segs = segment_line(work)
    if not segs:
        return PlaceResult()
    clauses = []
    for s in segs:
        if s.hard_before or not clauses:
            clauses.append([s])
        else:
            clauses[-1].append(s)

    def run(allow_city):
        for ci in range(len(clauses) - 1, -1, -1):
            r = _peel_clause(clauses[ci], work, allow_city)
            if r is not None:
                r.others = []
                for oj, oc in enumerate(clauses):
                    if oj == ci:
                        continue
                    a, b = oc[0].start, oc[-1].end
                    r.others.append({"text": text[a:b].strip(), "span": (a, b),
                                     "sep": (oc[0].sep_before if oj > ci else clauses[ci][0].sep_before),
                                     "side": "right" if oj > ci else "left"})
                return r
        return None

    r = run(False)
    if r is None and city_only:
        r = run(True)
    return r or PlaceResult()
