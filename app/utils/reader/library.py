"""
LibraryIndex (Resolver v2, chunk 3, 2026-10-03).

The reader's view of the user's own library: the Artist (act), Musician,
Membership, Venue and Event rows, loaded once per run and handed in, so the
decoder stays pure and testable without an app context.

    LibraryIndex.empty()                         a new user's library
    LibraryIndex.from_dicts(artists=[...], ...)  tests
    LibraryIndex.from_session(db.session)        the app path (see current_library())
    LibraryIndex.from_snapshot(path)             a read-only snapshot file (evaluation)

Evaluation must never score against the full library, because the answers ARE
the library. `without_performance(pid)` returns a view with every row hidden
that only that performance uses (its venue, event and act, and the musicians
who belong to no other visible act): the leave-one-out index.

Matching is by normalised key: accents, case, "&"/"and", punctuation and a
leading "The" are ignored. Acts also match on the act core (see
app.utils.artists._act_key), so "Aoife O'Donovan" finds "Aoife O'Donovan Band".
"""
import re
import sqlite3
import unicodedata
from functools import lru_cache


def norm_key(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^the ", "", s)


def person_key(s):
    """norm_key with the spaces gone, so "Al DiMeola" meets "Al Di Meola"."""
    return norm_key(s).replace(" ", "")


_ACT_NOISE = {"band", "trio", "quartet", "quintet", "group", "ensemble", "orchestra",
              "the", "and", "with", "featuring", "feat"}


def act_core(s):
    """The words that identify an act (same idea as artists._act_key, accent-folded)."""
    words = norm_key(s).split()
    core = [w for w in words if w not in _ACT_NOISE]
    return " ".join(core or words)


class LibraryIndex:
    def __init__(self):
        self.artists = {}       # id -> {"name", "members": [musician ids], "perfs": set}
        self.musicians = {}     # id -> {"name", "acts": set(artist ids)}
        self.venues = {}        # id -> {"name", "city", "state", "country", "perfs": set}
        self.events = {}        # id -> {"name", "venue_id", "perfs": set}
        self.perf_ref = {}      # performance id -> (artist id, venue id, event id)
        self._k_artist = {}     # exact key -> [ids]
        self._k_core = {}       # act core -> [ids]
        self._k_musician = {}
        self._k_venue = {}
        self._k_event = {}
        self._member_sets = {}  # frozenset(musician keys) -> [artist ids]
        self._hidden = {"artist": frozenset(), "musician": frozenset(),
                        "venue": frozenset(), "event": frozenset()}
        self._venue_names = None

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def empty(cls):
        return cls()

    @classmethod
    def from_rows(cls, artists, musicians, memberships, venues, events, performances):
        """artists (id, name); musicians (id, name); memberships (artist_id, musician_id);
        venues (id, name, city, state, country); events (id, name, venue_id);
        performances (id, artist_id, venue_id, event_id)."""
        from app.utils.venues import is_placeholder_venue_name
        ix = cls()
        for aid, name in artists:
            if name:
                ix.artists[aid] = {"name": name, "members": [], "perfs": set()}
        for mid, name in musicians:
            if name:
                ix.musicians[mid] = {"name": name, "acts": set()}
        for aid, mid in memberships:
            if aid in ix.artists and mid in ix.musicians:
                if mid not in ix.artists[aid]["members"]:
                    ix.artists[aid]["members"].append(mid)
                ix.musicians[mid]["acts"].add(aid)
        for vid, name, city, state, country in venues:
            if name and not is_placeholder_venue_name(name):
                ix.venues[vid] = {"name": name, "city": city, "state": state,
                                  "country": country, "perfs": set()}
        for eid, name, vid in events:
            if name:
                ix.events[eid] = {"name": name, "venue_id": vid, "perfs": set()}
        for pid, aid, vid, eid in performances:
            ix.perf_ref[pid] = (aid, vid, eid)
            if aid in ix.artists:
                ix.artists[aid]["perfs"].add(pid)
            if vid in ix.venues:
                ix.venues[vid]["perfs"].add(pid)
            if eid in ix.events:
                ix.events[eid]["perfs"].add(pid)
        ix._finish()
        return ix

    @classmethod
    def from_dicts(cls, artists=(), musicians=(), venues=(), events=()):
        """Test construction. artists: [{"name", "members": [names]}] (or plain names);
        musicians: [names]; venues: [{"name", "city", "state", "country"}] (or names);
        events: [{"name", "venue": venue name or None}] (or names)."""
        mus = {}

        def mid(name):
            return mus.setdefault(name, len(mus) + 1)
        a_rows, m_rows, ms = [], [], []
        for i, a in enumerate(artists, 1):
            a = {"name": a} if isinstance(a, str) else a
            a_rows.append((i, a["name"]))
            for n in a.get("members", ()):
                ms.append((i, mid(n)))
        for n in musicians:
            mid(n)
        m_rows = [(i, n) for n, i in mus.items()]
        v_rows, vids = [], {}
        for i, v in enumerate(venues, 1):
            v = {"name": v} if isinstance(v, str) else v
            v_rows.append((i, v["name"], v.get("city"), v.get("state"), v.get("country")))
            vids[v["name"]] = i
        e_rows = []
        for i, e in enumerate(events, 1):
            e = {"name": e} if isinstance(e, str) else e
            e_rows.append((i, e["name"], vids.get(e.get("venue"))))
        return cls.from_rows(a_rows, m_rows, ms, v_rows, e_rows, [])

    @classmethod
    def from_session(cls, session):
        from sqlalchemy import text
        q = lambda sql: [tuple(r) for r in session.execute(text(sql)).fetchall()]
        return cls.from_rows(
            q("select id, name from artist"), q("select id, name from musician"),
            q("select artist_id, musician_id from membership"),
            q("select id, name, city, state, country from venue"),
            q("select id, name, venue_id from event"),
            q("select id, artist_id, venue_id, event_id from performance"))

    @classmethod
    def from_snapshot(cls, path):
        """A snapshot file, opened read-only in place."""
        con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            q = lambda sql: con.execute(sql).fetchall()
            return cls.from_rows(
                q("select id, name from artist"), q("select id, name from musician"),
                q("select artist_id, musician_id from membership"),
                q("select id, name, city, state, country from venue"),
                q("select id, name, venue_id from event"),
                q("select id, artist_id, venue_id, event_id from performance"))
        finally:
            con.close()

    def _finish(self):
        for aid, a in self.artists.items():
            self._k_artist.setdefault(norm_key(a["name"]), []).append(aid)
            self._k_core.setdefault(act_core(a["name"]), []).append(aid)
            if len(a["members"]) >= 2:
                ks = frozenset(person_key(self.musicians[m]["name"]) for m in a["members"])
                self._member_sets.setdefault(ks, []).append(aid)
        for mid, m in self.musicians.items():
            self._k_musician.setdefault(person_key(m["name"]), []).append(mid)
        for vid, v in self.venues.items():
            self._k_venue.setdefault(norm_key(v["name"]), []).append(vid)
        for eid, e in self.events.items():
            self._k_event.setdefault(norm_key(e["name"]), []).append(eid)

    @property
    def is_empty(self):
        return not (self.artists or self.musicians or self.venues or self.events)

    # ── leave-one-out ───────────────────────────────────────────────────────
    def without_performance(self, pid):
        """A view of this index with every row hidden that only performance `pid` uses."""
        ref = self.perf_ref.get(pid)
        view = LibraryIndex.__new__(LibraryIndex)
        view.__dict__.update(self.__dict__)
        hid = {"artist": set(), "musician": set(), "venue": set(), "event": set()}
        if ref:
            aid, vid, eid = ref
            if aid in self.artists and self.artists[aid]["perfs"] <= {pid}:
                hid["artist"].add(aid)
                for m in self.artists[aid]["members"]:
                    if self.musicians[m]["acts"] <= hid["artist"]:
                        hid["musician"].add(m)
            if vid in self.venues and self.venues[vid]["perfs"] <= {pid}:
                hid["venue"].add(vid)
            if eid in self.events and self.events[eid]["perfs"] <= {pid}:
                hid["event"].add(eid)
        view._hidden = {k: frozenset(v) for k, v in hid.items()}
        return view

    # ── lookups (all hide-aware) ────────────────────────────────────────────
    def _vis(self, kind, ids):
        h = self._hidden[kind]
        return [i for i in ids if i not in h]

    def artist_match(self, text):
        """-> (name, quality) with quality "exact" | "core", or None."""
        k = norm_key(text)
        if not k:
            return None
        ids = self._vis("artist", self._k_artist.get(k, ()))
        if ids:
            return self.artists[ids[0]]["name"], "exact"
        c = act_core(text)
        ids = self._vis("artist", self._k_core.get(c, ())) if c else []
        if ids:
            return self.artists[ids[0]]["name"], "core"
        return None

    def musician_match(self, text):
        ids = self._vis("musician", self._k_musician.get(person_key(text), ()))
        return self.musicians[ids[0]]["name"] if ids else None

    def venue_match(self, text):
        ids = self._vis("venue", self._k_venue.get(norm_key(text), ()))
        return self.venues[ids[0]] if ids else None

    def event_match(self, text):
        ids = self._vis("event", self._k_event.get(norm_key(text), ()))
        return self.events[ids[0]] if ids else None

    def event_venue(self, event):
        """The venue row an event is linked to, or None (also None when that venue is hidden)."""
        if not event or event.get("venue_id") is None:
            return None
        vid = event["venue_id"]
        if vid in self._hidden["venue"]:
            return None
        return self.venues.get(vid)

    def act_for_members(self, names):
        """The act whose member set equals `names` (two or more), or None. -> act name."""
        ks = frozenset(person_key(n) for n in names if person_key(n))
        if len(ks) < 2:
            return None
        ids = self._vis("artist", self._member_sets.get(ks, ()))
        return self.artists[ids[0]]["name"] if ids else None

    def artist_names(self):
        h = self._hidden["artist"]
        return [a["name"] for i, a in self.artists.items() if i not in h]

    def venue_names(self):
        h = self._hidden["venue"]
        return [v["name"] for i, v in self.venues.items() if i not in h]

    def venue_names_in(self, text):
        """Visible venue names of two or more words contained whole-word in `text`
        (normalised keys). One-word names only match the whole text (venue_match)."""
        if self._venue_names is None:
            idx = {}
            for k in self._k_venue:
                w = k.split()
                if len(w) >= 2 and len(k) >= 7:
                    idx.setdefault(w[0], []).append(k)
            self._venue_names = idx
        key = norm_key(text)
        words = key.split()
        padded = f" {key} "
        out = []
        for w in set(words):
            for k in self._venue_names.get(w, ()):
                if f" {k} " in padded and self._vis("venue", self._k_venue[k]):
                    out.append(k)
        return out


# ── the app path ─────────────────────────────────────────────────────────────

_cache = {"token": None, "index": None}


def current_library():
    """The library as the app's database holds it, built once and reused while the
    row counts and newest update are unchanged. An empty index outside an app
    context or on any database error: the reader never fails because of it."""
    try:
        from sqlalchemy import text
        from app.extensions import db
        s = db.session
        token = tuple(s.execute(text(
            "select (select count(*) from artist), (select count(*) from musician), "
            "(select count(*) from membership), (select count(*) from venue), "
            "(select count(*) from event), "
            "(select coalesce(max(updated_at), '') from artist), "
            "(select coalesce(max(updated_at), '') from venue), "
            "(select coalesce(max(updated_at), '') from event), "
            "(select coalesce(max(updated_at), '') from musician), "
            # membership has no timestamp: a relink changes its ids
            "(select coalesce(max(id), 0) || ':' || coalesce(sum(artist_id * 1000003 + musician_id), 0) from membership), "
            "(select coalesce(sum(coalesce(venue_id, 0) * 1000003 + id), 0) from event)")).fetchone())
        if _cache["token"] != token:
            _cache["index"] = LibraryIndex.from_session(s)
            _cache["token"] = token
        return _cache["index"]
    except Exception:  # noqa: BLE001 -- no app context, no tables, locked db: fall back to empty
        return LibraryIndex.empty()
