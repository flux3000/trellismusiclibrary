"""app/utils/dim_index.py -- the extra fields the dimension INDEX pages read.

The five index pages (Artists, Musicians, Venues, Events, Genres) are rows, not
tiles, and a row carries more than a tile did: a description excerpt, a year
span, who played there, who is in the band. The list endpoints also feed the
sidebar on every render, so those fields are NOT added to the default payload.
They ride behind `?index=1`, and every function here is one batch of grouped
queries over the whole table, never a query per row.

Peers: each function takes `rec_ids`. None means the whole library (the local
owner). A set means "count and span only over these recordings", which is how
share.py scopes everything (peer_visible_recording_ids). With a set, a row can
only ever mention a performance, artist or year that the peer's own visible
set reaches.

Only text that is already stored is used. Nothing here fetches anything.
"""

import json
import re

from sqlalchemy import func

from app.extensions import db

EXCERPT_CHARS = 400

_CITE_RE = re.compile(r"</?cite[^>]*>", re.IGNORECASE)
_REF_RE = re.compile(r"\[\s*\d+(?:\s*[,–-]\s*\d+)*\s*\]")


def excerpt(text, limit=EXCERPT_CHARS):
    """Plain, citation-free, single-paragraph start of a stored description.
    '' when there is nothing. Cut at a word boundary, with an ellipsis, so the
    page's 3-line clamp is never what decides where the text stops mid-word."""
    if not text:
        return ""
    out = _REF_RE.sub("", _CITE_RE.sub("", str(text)))
    out = re.sub(r"\s+", " ", out).strip()
    out = re.sub(r"\s+([.,;:!?])", r"\1", out)
    if len(out) <= limit:
        return out
    cut = out[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut + "…"


def _first_text(*candidates):
    for c in candidates:
        t = excerpt(c)
        if t:
            return t
    return ""


def _dossier_biography(raw):
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        return ""
    return (data.get("biography") or "") if isinstance(data, dict) else ""


def _perf_stats(rec_ids):
    """{performance_id: [artist_id, venue_id, event_id, y, m, d, n_recordings]}
    for performances with at least one recording in `rec_ids` (every recording
    when None). Two grouped queries, whatever the library size."""
    from app.models.performance import Performance
    from app.models.recording import Recording

    counts = {}
    for rid, pid in db.session.query(Recording.id, Recording.performance_id).filter(
            Recording.is_published.is_(True)):
        if pid is None:
            continue
        if rec_ids is not None and rid not in rec_ids:
            continue
        counts[pid] = counts.get(pid, 0) + 1
    if not counts:
        return {}
    out = {}
    for row in db.session.query(
            Performance.id, Performance.artist_id, Performance.venue_id,
            Performance.event_id, Performance.start_year,
            Performance.start_month, Performance.start_day):
        if row[0] in counts:
            out[row[0]] = [row[1], row[2], row[3], row[4], row[5], row[6],
                           counts[row[0]]]
    return out


def _span(perfs):
    ys = [p[3] for p in perfs if p[3]]
    return (min(ys), max(ys)) if ys else (None, None)


def _top_names(perfs, names, n):
    """Artist names ranked by recordings across `perfs`, then name."""
    tally = {}
    for p in perfs:
        tally[p[0]] = tally.get(p[0], 0) + p[6]
    ids = sorted(tally, key=lambda a: (-tally[a], (names.get(a) or "").lower()))
    return [names[a] for a in ids[:n] if names.get(a)], len(tally)


def _artist_names():
    from app.models.artist import Artist
    return dict(db.session.query(Artist.id, Artist.name).all())


def artist_extras(rec_ids=None):
    """{artist_id: {area, excerpt, first_year, last_year, image_id}}"""
    from app.models.artist import Artist
    from app.models.artist_image import ArtistImage

    by_artist = {}
    for p in _perf_stats(rec_ids).values():
        by_artist.setdefault(p[0], []).append(p)

    images = {}
    for aid, iid in (db.session.query(ArtistImage.artist_id, ArtistImage.id)
                     .order_by(ArtistImage.artist_id,
                               ArtistImage.is_primary.desc(),
                               ArtistImage.sort_order, ArtistImage.id)):
        images.setdefault(aid, iid)

    out = {}
    for aid, bio, dossier, area in db.session.query(
            Artist.id, Artist.bio, Artist.dossier_json, Artist.mb_area):
        y0, y1 = _span(by_artist.get(aid, []))
        out[aid] = {
            "area":       area or None,
            "excerpt":    _first_text(bio, _dossier_biography(dossier)),
            "first_year": y0,
            "last_year":  y1,
            "image_id":   images.get(aid),
        }
    return out


def musician_extras(rec_ids=None, artist_ids=None):
    """{musician_id: {instruments, artists, artist_count, recording_count,
    excerpt}}. `artist_ids` restricts which acts count (a peer's visible acts)."""
    from app.models.musician import Musician, Membership

    by_artist = {}
    for p in _perf_stats(rec_ids).values():
        by_artist.setdefault(p[0], []).append(p)
    rec_by_artist = {a: sum(p[6] for p in ps) for a, ps in by_artist.items()}
    names = _artist_names()

    stints = {}
    for mid, aid, instrument in (db.session.query(
            Membership.musician_id, Membership.artist_id, Membership.instrument)
            .order_by(Membership.order, Membership.id)):
        if artist_ids is not None and aid not in artist_ids:
            continue
        stints.setdefault(mid, []).append((aid, instrument))

    out = {}
    for mid, bio in db.session.query(Musician.id, Musician.bio):
        rows = stints.get(mid, [])
        instruments = []
        for _aid, instrument in rows:
            for part in (instrument or "").split(","):
                part = part.strip()
                if part and part not in instruments:
                    instruments.append(part)
        aids = list(dict.fromkeys(a for a, _i in rows))
        ranked = sorted(aids, key=lambda a: (-rec_by_artist.get(a, 0),
                                             (names.get(a) or "").lower()))
        out[mid] = {
            "instruments":     instruments,
            "artists":         [names[a] for a in ranked[:3] if names.get(a)],
            "artist_count":    len(aids),
            "recording_count": sum(rec_by_artist.get(a, 0) for a in aids),
            "excerpt":         excerpt(bio),
        }
    return out


def engineer_extras(rec_ids=None):
    """{engineer_id: {recording_count, excerpt}} over LIVE recordings credited in
    any role. `rec_ids` restricts which recordings count (a peer's visible set);
    None is the whole library, published recordings only."""
    from app.models.engineer import Engineer
    from app.models.recording import Recording

    q = db.session.query(Recording.id, Recording.recording_engineer_id,
                         Recording.transfer_engineer_id, Recording.mastering_engineer_id
                         ).filter(Recording.kind != "studio")
    if rec_ids is None:
        q = q.filter(Recording.is_published.is_(True))
    counts = {}
    for rid, *eids in q:
        if rec_ids is not None and rid not in rec_ids:
            continue
        for eid in {x for x in eids if x is not None}:
            counts[eid] = counts.get(eid, 0) + 1
    return {eid: {"recording_count": counts.get(eid, 0), "excerpt": excerpt(bio)}
            for eid, bio in db.session.query(Engineer.id, Engineer.bio)}


def venue_extras(rec_ids=None):
    """{venue_id: {first_year, last_year, recording_count, excerpt}}"""
    from app.models.venue import Venue

    by_venue = {}
    for p in _perf_stats(rec_ids).values():
        if p[1]:
            by_venue.setdefault(p[1], []).append(p)

    out = {}
    for vid, bio, history in db.session.query(Venue.id, Venue.bio, Venue.history):
        ps = by_venue.get(vid, [])
        y0, y1 = _span(ps)
        out[vid] = {
            "first_year":      y0,
            "last_year":       y1,
            "recording_count": sum(p[6] for p in ps),
            "excerpt":         _first_text(bio, history),
        }
    return out


def event_extras():
    """{event_id: {first, last, show_venue, show_place, top_artists,
    artist_count, venues, venue_count, excerpt}}. Local only: events have no
    share route.

    An event row often stores no dates, venue or place of its own (they are
    entered on the performances), and an event spans many performances across
    dates and venues, so everything here is read from ALL of its performances,
    recorded or not. `first`/`last` are [y, m, d] of the earliest start and the
    latest end (or start); `venues` are "Name, City" strings, de-duplicated;
    `top_artists` is the busiest acts first, capped, with `artist_count` the
    true total."""
    from app.models.event import Event
    from app.models.performance import Performance
    from app.models.venue import Venue

    from app.models.recording import Recording
    per_perf = {}
    for pid, in db.session.query(Recording.performance_id).filter(
            Recording.performance_id.isnot(None), Recording.is_published.is_(True)):
        per_perf[pid] = per_perf.get(pid, 0) + 1

    by_event = {}
    for row in db.session.query(
            Performance.id, Performance.artist_id, Performance.venue_id,
            Performance.event_id, Performance.city, Performance.state,
            Performance.start_year, Performance.start_month, Performance.start_day,
            Performance.end_year, Performance.end_month, Performance.end_day,
    ).filter(Performance.event_id.isnot(None)):
        by_event.setdefault(row[3], []).append(row)

    names = _artist_names()
    venues = {v.id: v for v in db.session.query(Venue.id, Venue.name, Venue.city, Venue.state)}

    def start_key(r):
        return (r[6] or 0, r[7] or 0, r[8] or 0)

    def end_key(r):
        return (r[9], r[10] or 0, r[11] or 0) if r[9] else start_key(r)

    def label(v):
        return ", ".join(x for x in (v.name, v.city) if x)

    out = {}
    for e in db.session.query(Event.id, Event.venue_id, Event.city, Event.state,
                              Event.notes, Event.history):
        ps = sorted(by_event.get(e.id, []), key=start_key)
        dated = [r for r in ps if r[6]]
        first = list(start_key(dated[0])) if dated else None
        last = list(max((end_key(r) for r in dated), default=None) or ()) if dated else None

        # Venues in order of first appearance, the event's own anchor first.
        vids = []
        if e.venue_id:
            vids.append(e.venue_id)
        for r in ps:
            if r[2] and r[2] not in vids:
                vids.append(r[2])
        vlist = [venues[i] for i in vids if i in venues]
        v = vlist[0] if vlist else None

        city, state = e.city, e.state
        if not city and v is not None:
            city, state = v.city, v.state
        if not city:
            city, state = next(((r[4], r[5]) for r in sorted(ps, key=lambda r: r[0])
                                if r[4]), (None, None))

        tally = {}
        for r in ps:
            tally[r[1]] = tally.get(r[1], 0) + per_perf.get(r[0], 0)
        aids = sorted(tally, key=lambda a: (-tally[a], (names.get(a) or "").lower()))
        aids = [a for a in aids if names.get(a)]
        out[e.id] = {
            "first":        first,
            "last":         last,
            "show_venue":   v.name if v is not None else None,
            "show_place":   ", ".join(x for x in (city, state) if x),
            "top_artists":  [names[a] for a in aids[:12]],
            "artist_count": len(aids),
            "venues":       [label(x) for x in vlist[:6]],
            "venue_count":  len(vlist),
            "excerpt":      _first_text(e.notes, e.history),
        }
    return out


def genre_faces(rec_ids=None, artist_ids=None, limit=8):
    """{genre_id: [{id, name, image_id}]}: up to `limit` artists per genre,
    photographed ones first, then the busiest. Names and ids only."""
    from app.models.artist import Artist
    from app.models.artist_image import ArtistImage

    rec_by_artist = {}
    for p in _perf_stats(rec_ids).values():
        rec_by_artist[p[0]] = rec_by_artist.get(p[0], 0) + p[6]
    images = {}
    for aid, iid in (db.session.query(ArtistImage.artist_id, ArtistImage.id)
                     .order_by(ArtistImage.artist_id,
                               ArtistImage.is_primary.desc(),
                               ArtistImage.sort_order, ArtistImage.id)):
        images.setdefault(aid, iid)

    groups = {}
    for aid, name, gid in db.session.query(Artist.id, Artist.name, Artist.genre_id):
        if gid is None:
            continue
        if artist_ids is not None and aid not in artist_ids:
            continue
        groups.setdefault(gid, []).append((aid, name))
    out = {}
    for gid, rows in groups.items():
        rows.sort(key=lambda r: (r[0] not in images, -rec_by_artist.get(r[0], 0),
                                 (r[1] or "").lower()))
        out[gid] = [{"id": a, "name": n, "image_id": images.get(a)}
                    for a, n in rows[:limit]]
    return out
