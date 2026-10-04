"""
Atlas schema, matching keys and fuzzy scoring (Resolver v2, chunk 4).

Shared by build.py (writes the file) and lookup.py (reads it), so the two can
never disagree about how a name becomes a key. Stdlib only.

Matching, cheapest first
    1. key       norm_key(name): accents, case, "&"/"and", punctuation and a
                 leading "The" are ignored (the same normaliser the library
                 index uses, imported from it, not copied).
    2. core      act_core(name), acts only: "Pat Metheny Group" meets "Pat Metheny".
    3. squashed  the key with its spaces removed, so "DiMeola" meets "Di Meola".
    4. fuzzy     a trigram index over the squashed key (SQLite FTS5, 3.34 or
                 later). Where trigram is unavailable the file carries a
                 bigram table instead (meta.fuzzy_mode = "bigram") and lookup
                 reads that. Same scoring either way.
"""
import difflib
import re

from app.utils.reader.library import act_core, norm_key, person_key   # noqa: F401  (re-exported)

SCHEMA_VERSION = 1

# Entity kinds that have a fuzzy index. Areas and musicians are matched by key only
# (the reader never asks for a fuzzy musician, so the file carries no index for it).
FUZZY_KINDS = ("place", "act", "event")

# FTS5 options for the trigram index. detail=none drops the position lists and
# columnsize=0 the per-row length table: lookup only ever asks "which rows contain this
# 3-gram", never a phrase or a length-normalised rank, and the two options cut the
# index to under half its size. Ranking is done in lookup.py (idf-weighted shared grams).
FTS_OPTIONS = "tokenize='trigram', detail=none, columnsize=0"

# kind -> (name table, foreign key column, entity table)
NAME_TABLES = {
    "place":    ("place_name",    "place_id",    "place"),
    "act":      ("act_name",      "act_id",      "act"),
    "musician": ("musician_name", "musician_id", "musician"),
    "area":     ("area_name",     "area_id",     "area"),
    "event":    ("event",         "id",          "event"),
}

DDL = """
create table meta (key text primary key, value text);

create table area (
    id integer primary key, mbid text, geonameid integer, name text not null,
    kind text, country text, region text, parent_id integer,
    lat real, lon real, population integer, source text);
create table area_name (
    id integer primary key, area_id integer not null, name text not null,
    key text not null, kind text);

create table place (
    id integer primary key, mbid text, wikidata text, name text not null,
    kind text, area_id integer, city text, region text, country text,
    lat real, lon real, begin_year integer, end_year integer, source text);
create table place_name (
    id integer primary key, place_id integer not null, name text not null,
    key text not null, start_year integer, end_year integer, kind text);
create table place_area (
    place_id integer not null, area_id integer not null,
    primary key (place_id, area_id)) without rowid;

create table act (
    id integer primary key, mbid text, name text not null, sort_name text,
    kind text, begin_year integer, end_year integer, country text,
    n_events integer not null default 0, n_release_groups integer not null default 0,
    popularity integer not null default 0);
create table act_name (
    id integer primary key, act_id integer not null, name text not null,
    key text not null, ck text not null, kind text);
create table musician (
    id integer primary key, mbid text, name text not null);
create table musician_name (
    id integer primary key, musician_id integer not null, name text not null,
    key text not null, kind text);
create table act_member (
    act_id integer not null, musician_id integer not null,
    start_year integer, end_year integer, instrument text);

create table event (
    id integer primary key, mbid text, name text not null, key text not null,
    kind text, begin_date text, end_date text, place_id integer, area_id integer,
    series text);
create table event_act (
    act_id integer not null, event_id integer not null,
    primary key (act_id, event_id)) without rowid;
"""

INDEXES = """
create index area_name_key on area_name(key);
create index place_name_key on place_name(key);
create index place_name_place on place_name(place_id);
create index place_wikidata on place(wikidata);
create index act_name_key on act_name(key);
create index act_name_ck on act_name(ck);
create index musician_name_key on musician_name(key);
create index act_member_act on act_member(act_id);
create index event_key on event(key);
"""

TABLES = ("meta", "area", "area_name", "place", "place_name", "place_area", "act",
          "act_name", "musician", "musician_name", "act_member", "event", "event_act")


def squash(s):
    return norm_key(s).replace(" ", "")


# ── fuzzy index ──────────────────────────────────────────────────────────────

def trigrams(sq):
    return {sq[i:i + 3] for i in range(len(sq) - 2)} if len(sq) >= 3 else set()


def bigrams(sq):
    return {sq[i:i + 2] for i in range(len(sq) - 1)} if len(sq) >= 2 else set()


def trigram_available():
    """True when this SQLite can build an FTS5 trigram index (3.34 or later)."""
    import sqlite3
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("create virtual table t using fts5(k, tokenize='trigram')")
            return True
        finally:
            con.close()
    except sqlite3.Error:
        return False


def fuzzy_query(grams):
    """FTS5 MATCH expression: any of these grams, each as a quoted phrase."""
    return " OR ".join('"' + g.replace('"', '""') + '"' for g in sorted(grams))


# ── scoring ──────────────────────────────────────────────────────────────────

def score_keys(q, c):
    """(score 0..1, how) of squashed query `q` against squashed candidate `c`."""
    if not q or not c:
        return 0.0, "none"
    if q == c:
        return 0.97, "squashed"
    short, long_ = (q, c) if len(q) <= len(c) else (c, q)
    best, how = 0.0, "fuzzy"
    if len(short) >= 5 and short in long_:
        best, how = min(0.95, 0.6 + 0.4 * len(short) / len(long_)), "contained"
    sm = difflib.SequenceMatcher(None, q, c, autojunk=False)
    if sm.real_quick_ratio() > best and sm.quick_ratio() > best:
        r = min(0.96, sm.ratio())
        if r > best:
            best, how = r, "fuzzy"
    return best, how


def year_of(value):
    """Leading four-digit year of an ISO-ish date string, or None."""
    m = re.match(r"^\s*(\d{4})", str(value or ""))
    return int(m.group(1)) if m else None
