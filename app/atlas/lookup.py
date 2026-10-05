"""
Atlas reader (Resolver v2, chunk 4, 2026-10-03).

Read-only and immutable: the file is opened `?mode=ro&immutable=1`, once, and the
connection is reused. A missing or unreadable Atlas is not an error: every lookup
returns no candidates and the reason is logged once, so the app and the reader
work exactly as they did before the Atlas existed.

    current_atlas()            the app's Atlas, or None
    set_atlas(path | None)     override (tests, the evaluation harness); None disables
    atlas.artist(text)         -> [Candidate]   acts, with aliases and search hints
    atlas.musician(text)       -> [Candidate]
    atlas.venue(text)          -> [Candidate]   places, former names included
    atlas.event(text)          -> [Candidate]   events and festival series
    atlas.area(text)           -> [Candidate]   cities and regions (key match only)
    atlas.event_place(artist, date)  -> [dict]  where the Atlas says this act played that day
    atlas.place_name_in_year(place_id, year)

A Candidate carries a score from 0 to 1 and how it matched ("exact", "core",
"squashed", "contained", "fuzzy"). The Atlas is evidence, never an authority: the
library outranks it (see reader/features.py) and no caller should treat a fuzzy
hit as a fact.

Path: $TRELLIS_ATLAS when set ("off" disables), else assets/atlas/atlas.sqlite
inside the app's resource folder (the repo from source, the bundle when packaged).
"""
import json
import logging
import math
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import schema as S

log = logging.getLogger("trellis.atlas")

ENV_VAR = "TRELLIS_ATLAS"
REL_PATH = Path("assets") / "atlas" / "atlas.sqlite"
_OFF = {"off", "none", "0", "false", "disabled"}
_ON = {"on", "1", "true", "yes", "enabled", "default"}

DEFAULT_MIN_SCORE = 0.85
_CANDIDATES = 40           # fuzzy rows pulled before scoring (best-ranked first; bigram fallback)
_POOL = 150                # the same for the trigram index, whose ranking is idf-weighted gram counts
                           # without a length term: a wider pool matches the old bm25 pick (measured, 2,500
                           # misspelled act names: 40 rows found 99% of what bm25 found, 150 found 101%)
_MIN_SHARED = 0.3          # share of trigrams a candidate must have in common before it is scored
_RAREST = 6                # grams used per fuzzy query
_CACHE_MAX = 4000


@dataclass
class Candidate:
    kind: str                 # "artist" | "musician" | "venue" | "event" | "area"
    id: int                   # row id in the Atlas (not a library id)
    name: str                 # canonical name
    matched: str              # the name that matched (may be a former name or an alias)
    score: float
    how: str
    name_kind: str = ""       # official | former | alias | search_hint
    start_year: int = None    # of the matched name, when it has dates
    end_year: int = None
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        return {"kind": self.kind, "id": self.id, "name": self.name, "matched": self.matched,
                "score": round(self.score, 3), "how": self.how, "name_kind": self.name_kind,
                "start_year": self.start_year, "end_year": self.end_year, "extra": dict(self.extra)}


# ── the reader ───────────────────────────────────────────────────────────────

class Atlas:
    def __init__(self, con, path):
        self.path = str(path)
        self._con = con
        self._lock = threading.Lock()
        self._cache = {}
        self._nrows = {}
        self.meta = {k: v for k, v in con.execute("select key, value from meta")}
        self.fuzzy_mode = self.meta.get("fuzzy_mode", "trigram")

    @classmethod
    def open(cls, path):
        """Open `path` read-only. Raises (OSError, sqlite3.Error, ValueError) when it cannot."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(str(p))
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro&immutable=1", uri=True,
                              check_same_thread=False)
        try:
            ver = dict(con.execute("select key, value from meta")).get("schema_version")
            if str(ver) != str(S.SCHEMA_VERSION):
                raise ValueError(f"atlas schema {ver!r}, this app reads {S.SCHEMA_VERSION}")
            if dict(con.execute("select key, value from meta")).get("fuzzy_mode", "trigram") == "trigram":
                # a SQLite too old for trigram would fail on the first fuzzy query; say so now
                if not S.trigram_available():
                    raise ValueError("this SQLite has no FTS5 trigram tokenizer (needs 3.34 or later)")
        except Exception:
            con.close()
            raise
        return cls(con, p)

    def close(self):
        with self._lock:
            self._con.close()

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _q(self, sql, args=()):
        with self._lock:
            return self._con.execute(sql, args).fetchall()

    def counts(self):
        return {t: self._q(f"select count(*) from {t}")[0][0] for t in S.TABLES if t != "meta"}

    # ── candidate retrieval ──────────────────────────────────────────────────
    def _rows(self, kind):
        """Rows in the fuzzy index of `kind` (the idf denominator), read once from meta.counts."""
        n = self._nrows.get(kind)
        if n is None:
            table = {"place": "place_name", "act": "act_name"}.get(kind, kind)
            try:
                n = int(json.loads(self.meta.get("counts") or "{}").get(table) or 0)
            except ValueError:
                n = 0
            n = self._nrows[kind] = n or self._q(f"select count(*) from {table}")[0][0] or 1
        return n

    def _fuzzy_ids(self, kind, sq):
        """Name-row ids whose squashed key shares the query's rarest grams."""
        if kind not in S.FUZZY_KINDS:
            return []
        if self.fuzzy_mode == "trigram":
            grams = S.trigrams(sq)
            if not grams:
                return []
            ph = ",".join("?" * len(grams))
            df = self._q(f"select term, doc from vocab_{kind} where term in ({ph})", sorted(grams))
            rare = [(t, d) for t, d in sorted(df, key=lambda r: (r[1], r[0])) if d > 0][:_RAREST]
            if not rare:
                return []
            n = self._rows(kind)
            score = {}
            for t, d in rare:
                w = math.log(1.0 + n / d)                   # idf: a rare gram says more
                for (rid,) in self._q(f"select rowid from fts_{kind} where fts_{kind} match ?",
                                      (S.fuzzy_query([t]),)):
                    score[rid] = score.get(rid, 0.0) + w
            best = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[:_POOL]
            return [rid for rid, _ in best]
        grams = S.bigrams(sq)
        if not grams:
            return []
        ph = ",".join("?" * len(grams))
        df = self._q(f"select g, n from gram_df_{kind} where g in ({ph})", sorted(grams))
        rare = [g for g, n in sorted(df, key=lambda r: (r[1], r[0]))][:_RAREST + 2]
        if not rare:
            return []
        ph = ",".join("?" * len(rare))
        rows = self._q(f"select rid, count(*) c from gram_{kind} where g in ({ph}) "
                       f"group by rid order by c desc limit {_CANDIDATES}", rare)
        return [r[0] for r in rows]

    def _names(self, kind, where, args):
        """Name rows as (name_id, entity_id, name, key, name_kind, start, end)."""
        if kind == "place":
            sql = ("select id, place_id, name, key, kind, start_year, end_year from place_name "
                   f"where {where}")
        elif kind == "act":
            sql = f"select id, act_id, name, key, kind, null, null from act_name where {where}"
        elif kind == "musician":
            sql = f"select id, musician_id, name, key, kind, null, null from musician_name where {where}"
        elif kind == "area":
            sql = f"select id, area_id, name, key, kind, null, null from area_name where {where}"
        else:
            sql = f"select id, id, name, key, kind, null, null from event where {where}"
        return self._q(sql, args)

    def _entity(self, kind, eid):
        if kind == "place":
            r = self._q("select name, kind, city, region, country, mbid, wikidata, begin_year, end_year "
                        "from place where id=?", (eid,))
            if not r:
                return None
            n, k, city, region, country, mbid, wd, by, ey = r[0]
            return n, {"place_kind": k, "city": city, "region": region, "country": country,
                       "mbid": mbid, "wikidata": wd, "begin_year": by, "end_year": ey}
        if kind == "act":
            r = self._q("select name, kind, country, popularity, n_events, mbid, begin_year, end_year "
                        "from act where id=?", (eid,))
            if not r:
                return None
            n, k, country, pop, ne, mbid, by, ey = r[0]
            return n, {"act_kind": k, "country": country, "popularity": pop, "n_events": ne,
                       "mbid": mbid, "begin_year": by, "end_year": ey}
        if kind == "musician":
            r = self._q("select name, mbid from musician where id=?", (eid,))
            return (r[0][0], {"mbid": r[0][1]}) if r else None
        if kind == "area":
            r = self._q("select name, kind, region, country, population, source from area where id=?", (eid,))
            if not r:
                return None
            n, k, region, country, pop, src = r[0]
            return n, {"area_kind": k, "region": region, "country": country, "population": pop, "source": src}
        r = self._q("select e.name, e.kind, e.begin_date, e.end_date, e.place_id, p.name, p.city, p.country, e.series "
                    "from event e left join place p on p.id = e.place_id where e.id=?", (eid,))
        if not r:
            return None
        n, k, b, e, pid, pn, pc, pco, series = r[0]
        return n, {"event_kind": k, "begin_date": b, "end_date": e, "place_id": pid, "place": pn,
                   "city": pc, "country": pco, "series": series}

    def find(self, kind, text, *, limit=5, min_score=DEFAULT_MIN_SCORE, fuzzy=True):
        """Candidates for `text`, best first. kind: place | act | musician | event | area."""
        ck_ = (kind, text, limit, min_score, fuzzy)
        hit = self._cache.get(ck_)
        if hit is not None:
            return list(hit)
        out = self._find(kind, text, limit, min_score, fuzzy)
        if len(self._cache) >= _CACHE_MAX:
            self._cache.clear()
        self._cache[ck_] = tuple(out)
        return out

    def _find(self, kind, text, limit, min_score, fuzzy):
        nk = S.norm_key(text)
        if not nk:
            return []
        scored = {}          # entity id -> (score, how, name row)

        def offer(row, score, how):
            cur = scored.get(row[1])
            if cur is None or (score, row[5] is not None or row[6] is not None, -len(row[2])) > \
                    (cur[0], cur[2][5] is not None or cur[2][6] is not None, -len(cur[2][2])):
                scored[row[1]] = (score, how, row)

        for row in self._names(kind, "key = ?", (nk,)):
            offer(row, 1.0, "exact")
        if kind == "act":
            ck = S.act_core(text)
            if ck:
                rows = self._q("select id, act_id, name, key, kind, null, null from act_name where ck = ?", (ck,))
                for row in rows:
                    offer(row, 0.95, "core")
        if not scored and fuzzy:
            sq = nk.replace(" ", "")
            ids = self._fuzzy_ids(kind, sq)
            if ids:
                qt = S.trigrams(sq)
                ph = ",".join("?" * len(ids))
                for row in self._names(kind, f"id in ({ph})", ids):
                    cand = row[3].replace(" ", "")
                    ct = S.trigrams(cand)
                    if qt and ct and len(qt & ct) / len(qt | ct) < _MIN_SHARED:
                        continue
                    sc, how = S.score_keys(sq, cand)
                    if sc >= min_score:
                        offer(row, sc, how)
        out = []
        for eid, (score, how, row) in scored.items():
            if score < min_score:
                continue
            ent = self._entity(kind, eid)
            if ent is None:
                continue
            name, extra = ent
            out.append(Candidate(kind={"act": "artist", "place": "venue"}.get(kind, kind), id=eid,
                                 name=name, matched=row[2], score=score, how=how,
                                 name_kind=row[4] or "", start_year=row[5], end_year=row[6], extra=extra))
        out.sort(key=lambda c: (-c.score, -(c.extra.get("popularity") or 0), c.name, c.id))
        return out[:limit]

    # ── the public lookups ───────────────────────────────────────────────────
    def artist(self, text, **kw):
        return self.find("act", text, **kw)

    def musician(self, text, **kw):
        kw.setdefault("fuzzy", False)
        return self.find("musician", text, **kw)

    def venue(self, text, **kw):
        """Places (venues, halls, stadiums, festival grounds); former names match too."""
        return self.find("place", text, **kw)

    def event(self, text, **kw):
        return self.find("event", text, **kw)

    def area(self, text, **kw):
        kw["fuzzy"] = False
        return self.find("area", text, **kw)

    def place_name_in_year(self, place_id, year):
        """The name a place carried in `year` (its canonical name when no dated name covers it)."""
        rows = self._q("select name, kind, start_year, end_year from place_name where place_id=?", (place_id,))
        order = {"official": 0, "former": 1, "alias": 2}
        cover = [r for r in rows if r[1] in ("official", "former")
                 and (r[2] is None or r[2] <= year) and (r[3] is None or year <= r[3])
                 and (r[2] is not None or r[3] is not None)]
        if cover:
            cover.sort(key=lambda r: (order.get(r[1], 3), r[0]))
            return cover[0][0]
        r = self._q("select name from place where id=?", (place_id,))
        return r[0][0] if r else None

    def place_info(self, place_id):
        """{"city", "region", "country", "kind"} of a place, or {}."""
        r = self._q("select city, region, country, kind from place where id=?", (place_id,))
        return dict(zip(("city", "region", "country", "kind"), r[0])) if r else {}

    def place_area_keys(self, place_id):
        """Normalised names of the areas a place sits in (its town, district, city, region, country)."""
        rows = self._q("select a.name from place_area pa join area a on a.id = pa.area_id where pa.place_id=?",
                       (place_id,))
        return {S.norm_key(r[0]) for r in rows}

    def place_keys(self, place_id):
        """Every normalised name this place has carried."""
        return [r[0] for r in self._q("select distinct key from place_name where place_id=?", (place_id,))]

    def members(self, act_id):
        """[(musician name, start_year, end_year, instrument)] of an act."""
        return self._q("select m.name, am.start_year, am.end_year, am.instrument from act_member am "
                       "join musician m on m.id = am.musician_id where am.act_id=? "
                       "order by am.start_year, m.name", (act_id,))

    def event_place(self, artist, date):
        """
        Shows the Atlas knows for this act on this day: [{"place_id", "place", "city",
        "country", "event", "kind", "date", "act", "exact"}], where `date` is (y, m, d),
        {"year", "month", "day"} or "YYYY-MM-DD" (month and day may be None; a partial
        date matches every show in that year or month and reports exact=False).
        Empty when the Atlas knows no such show.
        """
        y, m, d = _date_parts(date)
        if not y:
            return []
        nk = S.norm_key(artist)
        if not nk:
            return []
        ck = S.act_core(artist)
        prefix = f"{y:04d}" + (f"-{m:02d}" if m else "") + (f"-{d:02d}" if m and d else "")
        rows = self._q(
            "select distinct e.id, e.name, e.begin_date, e.end_date, p.id, p.name, p.city, p.country, a.name, e.kind "
            "from act_name n join act a on a.id = n.act_id "
            "join event_act ea on ea.act_id = n.act_id join event e on e.id = ea.event_id "
            "left join place p on p.id = e.place_id "
            "where (n.key = ? or n.ck = ?) and e.begin_date like ?",
            (nk, ck or nk, f"{y:04d}%"))
        out = []
        for eid, ename, b, e, pid, pname, city, country, aname, ekind in rows:
            if not _covers(b, e, prefix):
                continue
            out.append({"event_id": eid, "event": ename, "kind": ekind, "date": b, "place_id": pid, "place": pname,
                        "city": city, "country": country, "act": aname, "exact": len(prefix) == 10})
        out.sort(key=lambda r: (not r["exact"], r["date"] or "", r["event_id"]))
        return out


def _date_parts(date):
    if date is None:
        return None, None, None
    if isinstance(date, str):
        parts = date.strip().split("-")
        nums = []
        for p in parts[:3]:
            try:
                nums.append(int(p))
            except ValueError:
                break
        nums += [None] * (3 - len(nums))
        return tuple(nums)
    if isinstance(date, dict):
        return date.get("year"), date.get("month"), date.get("day")
    t = tuple(date) + (None, None, None)
    return t[0], t[1], t[2]


def _covers(begin, end, prefix):
    """Does the event [begin, end] (partial ISO dates) fall under `prefix`'s day, month or year?"""
    if not begin:
        return False
    if len(prefix) < 10:
        return begin.startswith(prefix)
    if begin == prefix:
        return True
    if len(begin) == 10 and end:
        last = end if len(end) == 10 else (end + "-31" if len(end) == 7 else end + "-12-31")
        return begin <= prefix <= last
    return False


# ── the app's Atlas ──────────────────────────────────────────────────────────

_UNSET = object()
_state = {"override": _UNSET, "atlas": None, "path": None, "tried": False, "error": None, "owned": False}
_state_lock = threading.Lock()


def default_path():
    try:
        from config import resource_dir
        return Path(resource_dir()) / REL_PATH
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parents[2] / REL_PATH


def atlas_path():
    """Where the Atlas is expected, or None when disabled."""
    if _state["override"] is not _UNSET:
        ov = _state["override"]
        return Path(ov.path) if isinstance(ov, Atlas) else (Path(ov) if ov else None)
    env = os.environ.get(ENV_VAR)
    if env is not None:
        if env.strip().lower() in _ON:
            return default_path()
        return None if env.strip().lower() in _OFF or not env.strip() else Path(env)
    return default_path()


def set_atlas(atlas_or_path):
    """Override the app's Atlas: a path, an Atlas, or None to disable it. Resets the cache."""
    with _state_lock:
        old, owned = _state["atlas"], _state["owned"]
        _state.update(override=atlas_or_path, atlas=None, path=None, tried=False, error=None, owned=False)
        if isinstance(atlas_or_path, Atlas):
            _state.update(atlas=atlas_or_path, tried=True, path=atlas_or_path.path)
        if old is not None and owned and old is not atlas_or_path:     # only a file this module opened
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass


def reset_atlas():
    """Forget the override and any cached connection (environment and default path apply again)."""
    set_atlas(None)
    with _state_lock:
        _state.update(override=_UNSET, atlas=None, path=None, tried=False, error=None, owned=False)


def current_atlas():
    """The app's Atlas, or None (disabled, missing or unreadable; logged once)."""
    if _state["tried"] and (_state["atlas"] is not None or _state["override"] is not _UNSET
                            or _state["path"] == _path_key()):
        return _state["atlas"]
    with _state_lock:
        path = atlas_path()
        _state.update(tried=True, atlas=None, error=None, path=_path_key())
        if path is None:
            _state["error"] = "disabled"
            return None
        try:
            _state["atlas"] = Atlas.open(path)
            _state["owned"] = True
        except FileNotFoundError:
            _state["error"] = f"missing: {path}"
            log.warning("Atlas not found at %s; reading without it", path)
        except (OSError, sqlite3.Error, ValueError) as e:
            _state["error"] = f"unreadable: {path} ({e})"
            log.warning("Atlas at %s could not be opened (%s); reading without it", path, e)
        return _state["atlas"]


def _path_key():
    p = atlas_path()
    return str(p) if p else None


def status():
    """One line for the startup self-test and the debug drawer: ("ok" | "missing" | "unreadable" | "disabled", text)."""
    a = current_atlas()
    if a is not None:
        c = a.counts()
        return "ok", (f"atlas ok - {a.path} - built {a.meta.get('build_date', '?')}, "
                      f"{c['act']} acts, {c['place']} places, {c['event']} events, "
                      f"fuzzy {a.fuzzy_mode}")
    err = _state["error"] or "disabled"
    return ("disabled" if err == "disabled" else "missing" if err.startswith("missing") else "unreadable"), \
        f"atlas {err}"
