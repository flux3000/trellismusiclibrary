"""
Billing assembler and field extractors (Resolver v2, chunk 3, 2026-10-03).

Turns the decoder's roles into the fields a person reads off an info file:

  artist   an ARTIST segment as written; or, when the file only names a tour
           banner and then "with" + a run of MEMBERs, the act those members make:
             library first: an act whose member set matches gives that act's name
             otherwise "A, B and C", tentative (a proposal, not what the file said)
  event    a festival-like (EVENT) segment
  stage    a "... Stage" (STAGE) segment
  venue    the VENUE segment; the physical place. A festival is not a venue: when
           only an event is found the venue comes from the library's event-to-venue
           link, else stays empty
  source   a SOURCE segment beats a keyword elsewhere (see source_from_roles)
  sets     SET_HEADER lines, as a carrier for each track's set

The evidence for each choice is returned with it, so the Resolver pane can later
show where a value came from and what the runner-up was.
"""
import re
from dataclasses import dataclass, field

from .decode import emission, _R
from .features import (VENUE_WORDS, is_festival_like, instrument_annotation, parse_set_label, person_shaped,
                       _PAREN_TAIL_RE, _HYPHEN_INSTR_RE, _INSTR)
from .library import LibraryIndex, norm_key

_ARTIST = _R["ARTIST"]
_VENUE_LEAD_RE = re.compile(r"^(?:recorded\s+live\s+at|recorded\s+at|live\s+at|live\s+in)(?:\s+|$)", re.I)
_SPLIT_AND_RE = re.compile(r"\s+(?:and|&)\s+", re.I)


@dataclass
class Reading:
    artist: str | None = None
    artist_how: str = ""            # "line" | "library act" | "assembled" | "members match" | "fallback"
    artist_tentative: bool = False
    event: str | None = None
    stage: str | None = None
    venue: str | None = None
    venue_how: str = ""             # "line" | "library event"
    members: list = field(default_factory=list)
    sets: list = field(default_factory=list)     # [{"label", "line"}]
    fields: dict = field(default_factory=dict)   # per-field evidence
    segments: list = field(default_factory=list)


def _clean(text):
    t = text.strip().strip("\"'“”‘’").strip()
    return re.sub(r"\s+", " ", t).strip(" -–—,;:|")


def _member_name(text):
    """A MEMBER unit's person name(s): instrument annotations removed, "A and B" split."""
    t = _PAREN_TAIL_RE.sub("", text).strip()
    m = _HYPHEN_INSTR_RE.search(t)
    if m and m.group(1).lower() in _INSTR and len(m.group(1)) >= 3:
        t = t[:m.start()].strip()
    t = _clean(t)
    if not t:
        return []
    parts = _SPLIT_AND_RE.split(t) if _SPLIT_AND_RE.search(t) else [t]
    out = []
    for p in parts:
        p = _clean(p)
        if person_shaped(p):
            out.append(p)
    return out


def _span_text(doc, units):
    return doc.text[units[0].start:units[-1].end]


def _artist_candidates(doc, dec):
    """ARTIST units grouped when contiguous on one line: [(units, score, decoded)].
    A group keeps the whole billing on its line: ARTIST, CONNECTOR and MEMBER
    units that follow it without a break are part of the artist text."""
    groups, cur = [], []
    for d in dec:
        if d.role == "ARTIST":
            if cur and (cur[-1].unit.line != d.unit.line):
                groups.append(cur)
                cur = []
            cur.append(d)
        elif cur and d.unit.line == cur[-1].unit.line and d.role in ("CONNECTOR", "MEMBER"):
            cur.append(d)
        else:
            if cur:
                groups.append(cur)
                cur = []
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        while g and g[-1].role != "ARTIST" and g[-1].role != "MEMBER":
            g = g[:-1]              # never end on a dangling connector
        us = [d.unit for d in g]
        out.append((us, max(d.score for d in g if d.role == "ARTIST"), g))
    return out


def _is_track_region(d):
    return bool(d.unit.feats.get("after_track_start"))


def _natural_join(names):
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def read_billing(doc, dec, library=None, title_case=None):
    """Assemble the fields from decoded units. `title_case` is ingest's title_case."""
    library = library or LibraryIndex.empty()
    tc = title_case or (lambda s: s)
    r = Reading()
    r.segments = dec

    # ── sets ──────────────────────────────────────────────────────────────
    for d in dec:
        if d.role == "SET_HEADER" and d.unit.kind == "sethdr":
            r.sets.append({"label": d.unit.set_label, "line": d.unit.line})

    # ── members ───────────────────────────────────────────────────────────
    members = []
    for d in dec:
        if d.role == "MEMBER" and not _is_track_region(d):
            for nm in _member_name(d.unit.text):
                if nm not in members:
                    members.append(nm)
    r.members = members

    # ── artist ────────────────────────────────────────────────────────────
    cands = _artist_candidates(doc, dec)
    best = None
    if cands:
        best = max(cands, key=lambda c: (c[1], -c[0][0].start))
    if best is not None:
        us, score, gd = best
        raw = _clean(_span_text(doc, us))
        r.artist = tc(raw)
        r.artist_how = "line"
        r.fields["artist"] = {"role": "ARTIST", "text": raw, "span": [us[0].start, us[-1].end],
                              "line": us[0].line, "score": round(score, 2)}
        # the library knows an act with exactly these members
        names = []
        if len(us) >= 2:
            for d in gd:
                if d.role == "CONNECTOR":
                    continue
                for nm in _SPLIT_AND_RE.split(_clean(d.unit.text)):
                    nm = _clean(nm)
                    if nm and nm not in names:
                        names.append(nm)
            act = library.act_for_members(names)
            if act:
                r.artist, r.artist_how = act, "library act"
                r.fields["artist"]["via"] = "library act with these members"
        am = library.artist_match(raw)
        if not (am and am[1] == "exact") and len(members) >= 2:
            act = library.act_for_members(members) or library.act_for_members(members + [raw])
            if act:
                r.artist, r.artist_how = act, "members match"
                r.fields["artist"]["via"] = "library act with these members"
    else:
        # a banner, then "with" and a run of members: the members are the act
        runs_ok = len(members) >= 2 and any(d.role == "CONNECTOR" for d in dec)
        if runs_ok:
            act = library.act_for_members(members)
            if act:
                r.artist, r.artist_how = act, "library act"
                r.fields["artist"] = {"role": "MEMBER", "text": ", ".join(members), "via": "library act with these members"}
            else:
                names = [tc(m) for m in members]
                r.artist, r.artist_how, r.artist_tentative = _natural_join(names), "assembled", True
                r.fields["artist"] = {"role": "MEMBER", "text": ", ".join(members), "via": "assembled from members",
                                      "tentative": True}
        else:
            # nothing decoded as an artist: a banner-like unit that still reads like one
            top = []
            for d in dec:
                u = d.unit
                if u.kind != "seg" or u.nb > 3 or d.role not in ("TITLE", "OTHER", "NOTES"):
                    continue
                e = emission(u)[_ARTIST]
                if e >= (1.0 if u.nb == 0 else 2.0):
                    top.append((e, -u.start, d))
            if top:
                _, _, d = max(top)
                raw = _clean(d.unit.text)
                if raw:
                    r.artist, r.artist_how = tc(raw), "fallback"
                    r.fields["artist"] = {"role": d.role, "text": raw, "span": [d.unit.start, d.unit.end],
                                          "line": d.unit.line, "score": round(d.score, 2), "via": "best guess"}

    # ── venue / event / stage ─────────────────────────────────────────────
    def best_unit(role):
        c = [d for d in dec if d.role == role and not _is_track_region(d)]
        return max(c, key=lambda d: (d.score, -d.unit.start)) if c else None

    ev = best_unit("EVENT")
    if ev is not None:
        r.event = tc(_clean(ev.unit.text))
        r.fields["event"] = {"role": "EVENT", "text": _clean(ev.unit.text), "span": [ev.unit.start, ev.unit.end],
                             "line": ev.unit.line, "score": round(ev.score, 2)}
    _BODY = ("block_later", "line_late", "source_kw", "recording_verb", "equip_word", "notes_word",
             "has_gt", "has_clock", "prose", "w8p")

    def header_stage(d):
        return d.role == "STAGE" and not any(d.unit.feats.get(k) for k in _BODY)
    cs = [d for d in dec if header_stage(d) and not _is_track_region(d)]
    st = max(cs, key=lambda d: (d.score, -d.unit.start)) if cs else None
    st_venue = None
    if st is not None:
        rem = re.sub(r"\s*\bstage\b\s*$", "", _clean(st.unit.text), flags=re.I).strip()
        if rem and {w.lower() for w in re.findall(r"[^\W\d_]+", rem)} & VENUE_WORDS:
            st_venue, st = rem, None        # "Town Park Stage": the place is the venue, the stage word is generic
    if st is not None:
        r.stage = tc(_clean(st.unit.text))
        r.fields["stage"] = {"role": "STAGE", "text": _clean(st.unit.text), "span": [st.unit.end - len(st.unit.text), st.unit.end],
                             "line": st.unit.line, "score": round(st.score, 2)}

    vd = best_unit("VENUE")
    if vd is not None:
        raw = _clean(_VENUE_LEAD_RE.sub("", vd.unit.text.strip()))
        raw = re.sub(r"^ft\.?\s+", "Fort ", raw, flags=re.I)
        _rem = re.sub(r"\s*\bstage\b\s*$", "", raw, flags=re.I).strip()
        if _rem and _rem != raw and {w.lower() for w in re.findall(r"[^\W\d_]+", _rem)} & VENUE_WORDS:
            raw = _rem              # "Town Park Stage": the generic stage word is dropped
        if raw and r.event and norm_key(raw) == norm_key(r.event):
            raw = ""                # the venue candidate is the event text: venue stays empty
        if raw and (is_festival_like(raw) or library.event_match(raw)) and not r.event:
            r.event = tc(raw)       # a festival never goes into Venue
            r.fields["event"] = {"role": "VENUE", "text": raw, "span": [vd.unit.start, vd.unit.end],
                                 "line": vd.unit.line, "score": round(vd.score, 2), "via": "festival-like venue segment"}
            raw = ""
        if raw:
            r.venue, r.venue_how = tc(raw), "line"
            r.fields["venue"] = {"role": "VENUE", "text": raw, "span": [vd.unit.start, vd.unit.end],
                                 "line": vd.unit.line, "score": round(vd.score, 2),
                                 "runner_up": {"value": vd.runner_up[0], "score": round(vd.runner_up[1], 2)}}
    if r.venue is None and st_venue:
        r.venue, r.venue_how = tc(st_venue), "line"
        r.fields["venue"] = {"role": "STAGE", "text": st_venue, "via": "place named in a stage line"}
    if r.venue is None and r.event:
        evrow = library.event_match(r.event)
        vrow = library.event_venue(evrow)
        if vrow and not is_festival_like(vrow["name"]):
            r.venue, r.venue_how = vrow["name"], "library event"
            r.fields["venue"] = {"role": "EVENT", "text": r.event, "via": "library event's venue"}
    return r
