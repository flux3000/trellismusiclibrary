"""
api/search.py — global search (IO-46 V1).

Routes:
  GET /api/search?q=              — all four groups, top `limit` each (omnibox)
  GET /api/search?q=&type=<group> — one group, paged (results page)

THE RULE (Ryan, 2026-08-18): musician, artist, date, venue, or any
combination. Track titles and provenance free text are OUT of V1 — see the
module docstring in app/utils/search.py before widening this.

Deliberately NOT in this story:

  * Publish gating. `is_published` does not exist yet (IO-57); this endpoint
    is @login_required and nothing more, so search and publishing stay
    independent stories.
  * Peer/remote scope. A peer search MUST filter every group through the
    visible-set helpers in app/utils/peer_access.py or it leaks holdings,
    and wiring all four correctly is a story of its own (IO-48). There is no
    peer route here on purpose — its absence is safer than a half-filtered
    one, because the peer blueprint's whole premise is that it is
    structurally incapable of exposing what it shouldn't.

The five existing typeahead endpoints (/api/artists/search, /api/musicians/
search, /api/venues/?q=, /api/events/search, /api/genres/?q=) are left alone.
They feed the ingest pickers via wirePickerDropdown() and answer a different
question — "which single row did you mean?" rather than "what's in the
library?" Folding them into this endpoint is an API-surface decision nobody
has asked for.
"""

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app.extensions import db
from app.models.musician import Musician, Membership
from app.models.performance import Performance
from app.models.artist import Artist
from app.models.quality import RecordingQuality
from app.models.recording import Recording
from app.models.venue import Venue
from app.utils import search as se
from app.utils.format import format_partial_date

bp = Blueprint("search", __name__)

# Dropdown shows a few per group; the results page pages properly.
DEFAULT_DROPDOWN_LIMIT = 5
MAX_LIMIT = 100


# ── Index construction ─────────────────────────────────────────────────────

def build_search_index():
    """
    Load every searchable row and precompute its match keys.

    Four column-level queries — never ORM objects — so no relationship is
    lazily walked per row and there is no N+1 hiding in the serialiser.

    Not cached, on purpose. Measured cost is ~30ms for a corpus several times
    larger than THE RULE leaves in scope, and a cache here would need an
    invalidation signal that every write path remembers to fire — a whole
    class of "I edited the venue and search still shows the old name" bugs
    bought for 30ms. This function is the single seam: if the corpus grows
    enough to need caching or FTS5, it gets replaced here and nothing else
    moves.
    """
    artists = [
        {"id": pid, "name": name, "sort_name": sort_name}
        for pid, name, sort_name in db.session.query(
            Artist.id, Artist.name, Artist.sort_name)
    ]

    # Musician → the acts they're a member of. Membership, NOT
    # performance_personnel (Ryan, 2026-08-18). Stint dates are deliberately
    # ignored: a person's connection to a band is catalog metadata about the
    # act, and narrowing it to the nights they demonstrably played gives the
    # same incoherent result that made peer musician visibility derive from
    # membership too.
    member_of = {}
    for musician_id, artist_id in db.session.query(
            Membership.musician_id, Membership.artist_id):
        member_of.setdefault(musician_id, []).append(artist_id)

    musicians = [
        {"id": aid, "name": name, "sort_name": sort_name,
         "artist_ids": member_of.get(aid, [])}
        for aid, name, sort_name in db.session.query(
            Musician.id, Musician.name, Musician.sort_name)
    ]

    venues = [
        {"id": vid, "name": name, "city": city, "state": state, "country": country}
        for vid, name, city, state, country in db.session.query(
            Venue.id, Venue.name, Venue.city, Venue.state, Venue.country)
    ]

    # Members per act, for the recording rows' musician dimension.
    members_of_artist = {}
    for artist_id, musician_name in (
            db.session.query(Membership.artist_id, Musician.name)
            .join(Musician, Musician.id == Membership.musician_id)):
        members_of_artist.setdefault(artist_id, []).append(musician_name)

    rows = (
        db.session.query(
            Recording.id, Recording.performance_id, Recording.source, Recording.quality,
            Performance.artist_id,
            Performance.start_year, Performance.start_month, Performance.start_day,
            Artist.name.label("artist_name"),
            Artist.sort_name.label("artist_sort_name"),
            Venue.id.label("venue_id"), Venue.name.label("venue_name"),
            Venue.city, Venue.state, Venue.country,
            RecordingQuality.listening_quality,
        )
        .join(Performance, Performance.id == Recording.performance_id)
        .outerjoin(Artist, Artist.id == Performance.artist_id)
        .outerjoin(Venue, Venue.id == Performance.venue_id)
        .outerjoin(RecordingQuality, RecordingQuality.recording_id == Recording.id)
        .all()
    )

    recordings = [
        {
            "id":                  r.id,
            "performance_id":      r.performance_id,
            "artist_id":        r.artist_id,
            "artist_name":      r.artist_name,
            "artist_sort_name": r.artist_sort_name,
            "musician_names":        members_of_artist.get(r.artist_id, []),
            "venue_id":            r.venue_id,
            "venue_name":          r.venue_name,
            "city":                r.city,
            "state":               r.state,
            "country":             r.country,
            "year":                r.start_year,
            "month":               r.start_month,
            "day":                 r.start_day,
            "source":              r.source,
            "quality":             r.quality,
            "listening_quality":   r.listening_quality,
        }
        for r in rows
    ]

    return se.build_index(artists, musicians, venues, recordings)


# ── Serialisation ──────────────────────────────────────────────────────────
#
# Every item carries its own `hash`. The destination of a result is a routing
# fact the server already knows, and deriving it again in the frontend is how
# "musician" (the person) and "artist" (the act) get wired to each other's
# page — they are different entities with similar names and adjacent routes.

def _artist_item(row, counts):
    return {
        "type": "artist", "id": row["id"], "name": row["name"],
        "recording_count": counts["artists"].get(row["id"], 0),
        "hash": f"#/artist/{row['id']}",
    }


def _musician_item(row, artist_names):
    return {
        "type": "musician", "id": row["id"], "name": row["name"],
        "member_of": [artist_names[p] for p in row.get("artist_ids", [])
                      if p in artist_names],
        "hash": f"#/musician/{row['id']}",
    }


def _venue_item(row, counts):
    return {
        "type": "venue", "id": row["id"], "name": row["name"],
        "city": row.get("city"), "state": row.get("state"),
        "country": row.get("country"),
        "recording_count": counts["venues"].get(row["id"], 0),
        "hash": f"#/venue/{row['id']}",
    }


def _recording_item(row):
    return {
        "type": "recording", "id": row["id"],
        "artist": row.get("artist_name"),
        "artist_id": row.get("artist_id"),
        "date": format_partial_date(row.get("year"), row.get("month"), row.get("day")),
        "venue": row.get("venue_name"),
        "city": row.get("city"), "state": row.get("state"),
        "source": row.get("source"),
        "quality": row.get("quality"),
        "listening_quality": row.get("listening_quality"),
        "hash": f"#/recording/{row['id']}",
    }


def _derived_counts(index):
    """Recording counts per act and per venue, free from the index we already built."""
    artists, venues = {}, {}
    for r in index["recordings"]:
        pid, vid = r.get("artist_id"), r.get("venue_id")
        if pid:
            artists[pid] = artists.get(pid, 0) + 1
        if vid:
            venues[vid] = venues.get(vid, 0) + 1
    return {"artists": artists, "venues": venues}


def _serialise(group_key, entries, index, counts):
    if group_key == "artists":
        return [_artist_item(e["row"], counts) for e in entries]
    if group_key == "venues":
        return [_venue_item(e["row"], counts) for e in entries]
    if group_key == "musicians":
        names = {p["id"]: p["name"] for p in index["artists"]}
        return [_musician_item(e["row"], names) for e in entries]
    return [_recording_item(e["row"]) for e in entries]


def _int_arg(name, default, lo, hi):
    """Read a bounded integer query arg, falling back rather than 400-ing."""
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


# ── Routes ─────────────────────────────────────────────────────────────────

@bp.route("")
@bp.route("/")
@login_required
def search():
    """
    Global search across act, person, venue and date.

    An empty query answers 200 with empty groups, not 400. The omnibox fires
    this on every debounced keystroke including the one that clears the box,
    and an error status for "the user deleted their query" would put a red
    state on a perfectly normal interaction.
    """
    q = request.args.get("q", "").strip()
    group_type = request.args.get("type")

    if group_type is not None and group_type not in se.GROUP_ORDER:
        return jsonify({"error": f"unknown type: {group_type}"}), 400

    index = build_search_index()
    result = se.run_search(index, q)
    counts = _derived_counts(index)

    if group_type:
        # Results page: one group, properly paged.
        limit = _int_arg("limit", 25, 1, MAX_LIMIT)
        offset = _int_arg("offset", 0, 0, 10_000)
        g = result["groups"][group_type]
        window = g["items"][offset:offset + limit]
        return jsonify({
            "query":      result["query"],
            "text_terms": result["text_terms"],
            "date_terms": result["date_terms"],
            "type":       group_type,
            "label":      g["label"],
            "total":      g["total"],
            "offset":     offset,
            "limit":      limit,
            "items":      _serialise(group_type, window, index, counts),
        })

    # Omnibox: a few from every group, plus honest totals so the dropdown can
    # say "and 31 more" rather than implying five is all there is.
    limit = _int_arg("limit", DEFAULT_DROPDOWN_LIMIT, 1, MAX_LIMIT)
    groups = []
    for key in se.GROUP_ORDER:
        g = result["groups"][key]
        if not g["total"]:
            continue                      # empty modules hide entirely
        groups.append({
            "type":  key,
            "label": g["label"],
            "total": g["total"],
            "items": _serialise(key, g["items"][:limit], index, counts),
        })

    return jsonify({
        "query":      result["query"],
        "text_terms": result["text_terms"],
        "date_terms": result["date_terms"],
        "total":      sum(g["total"] for g in groups),
        "groups":     groups,
    })
