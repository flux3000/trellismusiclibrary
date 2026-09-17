"""
utils/artists.py — resolve/create Artists (acts) and their member Musicians.

Shared by ingest, the Add/Edit forms, and performance reassignment.

The Artist (the billed act) is the core entity. Member Musicians (people) are
OPTIONAL — an Artist may have zero members. Members are only added in special
cases: a one-off collaboration of otherwise-distinct musicians, or deliberate
personnel archiving. Nothing is auto-seeded from the artist's name.
"""

from sqlalchemy import func
from app.extensions import db
from app.models.artist import Artist
from app.models.musician import Musician, Membership


def resolve_or_create_musician(name):
    """Find a Musician (person) by name (case-insensitive) or create it."""
    name = (name or "").strip()
    if not name:
        return None
    a = db.session.query(Musician).filter(func.lower(Musician.name) == name.lower()).first()
    if not a:
        a = Musician(name=name)
        db.session.add(a)
        db.session.flush()
    return a


def _is_unbounded(membership):
    """True if a stint row has no date bounds at all ('always a member')."""
    return not any([
        membership.start_year, membership.start_month, membership.start_day,
        membership.end_year, membership.end_month, membership.end_day,
    ])


def set_artist_members(artist, member_names):
    """
    Sync an artist's roster to the given ordered list of names — the plain
    "list of names" editing path (Artist page, ingest). Covers the 90% case
    with no stint dates involved; see Per-Show Personnel design doc §7.6.

    STINT-SAFE (2026-07-18 rewrite): the old version deleted and recreated the
    whole roster on every call, which would silently destroy stint date bounds
    (e.g. Mickey Hart's two Membership rows — 1967-71 and 1974-95 — would
    collapse into one row and both sets of dates would vanish). Instead:

      - a name already linked to this artist keeps ALL of its existing
        stint row(s) untouched; only its `order` moves to the new position
        (order is a per-PERSON concept — the earliest stint's row carries it,
        per the design doc's dedupe rule)
      - a brand-new name gets one fresh unbounded stint row (NULL bounds =
        "always a member," identical to pre-personnel behavior)
      - a name dropped from the list is deleted ONLY if it has exactly one
        stint row and that row is fully unbounded (nothing to lose). A person
        with real stint history (dated bounds, or more than one stint) is
        left in place rather than silently destroyed. To actually end
        someone's tenure, put an end date on their stint — don't just drop
        them from this list.

    An empty list clears every member with no stint history — an Artist
    with zero Musicians is still valid.
    """
    names, seen = [], set()
    for n in (member_names or []):
        n = (n or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            names.append(n)

    existing = db.session.query(Membership).filter_by(artist_id=artist.id).all()
    by_musician_id = {}
    for m in existing:
        by_musician_id.setdefault(m.musician_id, []).append(m)

    kept_musician_ids = set()
    for i, name in enumerate(names):
        musician = resolve_or_create_musician(name)
        kept_musician_ids.add(musician.id)
        stints = by_musician_id.get(musician.id)
        if stints:
            earliest = min(stints, key=lambda m: m.order)
            earliest.order = i
        else:
            db.session.add(Membership(artist_id=artist.id, musician_id=musician.id, order=i))

    for musician_id, stints in by_musician_id.items():
        if musician_id in kept_musician_ids:
            continue
        if len(stints) == 1 and _is_unbounded(stints[0]):
            db.session.delete(stints[0])
        # else: real stint history on a now-dropped name — leave it in place.

    db.session.flush()


def add_membership_stint(artist, musician_name, order=None,
                          start_year=None, start_month=None, start_day=None,
                          end_year=None, end_month=None, end_day=None):
    """
    Add a NEW stint row linking `musician_name` to `artist`. Does not touch
    any existing stint(s) already on record for that person — this is how a
    second stint (e.g. Mickey Hart's post-1974 return) gets recorded without
    disturbing the first. `order` defaults to after the current max.
    """
    musician = resolve_or_create_musician(musician_name)
    if order is None:
        max_order = db.session.query(func.max(Membership.order)).filter_by(
            artist_id=artist.id).scalar()
        order = (max_order + 1) if max_order is not None else 0
    m = Membership(
        artist_id=artist.id, musician_id=musician.id, order=order,
        start_year=start_year, start_month=start_month, start_day=start_day,
        end_year=end_year, end_month=end_month, end_day=end_day,
    )
    db.session.add(m)
    db.session.flush()
    return m


def update_membership_stint_bounds(membership_id, start_year=None, start_month=None,
                                    start_day=None, end_year=None, end_month=None,
                                    end_day=None):
    """Edit one stint's date bounds in place. Does not affect a person's other stints."""
    m = db.session.get(Membership, membership_id)
    if not m:
        return None
    m.start_year, m.start_month, m.start_day = start_year, start_month, start_day
    m.end_year, m.end_month, m.end_day = end_year, end_month, end_day
    db.session.flush()
    return m


def remove_membership_stint(membership_id):
    """Delete exactly one stint row — not the whole person's membership history."""
    m = db.session.get(Membership, membership_id)
    if not m:
        return False
    db.session.delete(m)
    db.session.flush()
    return True


def resolve_or_create_artist(name, member_names=None):
    """
    Find an Artist by name (case-insensitive) or create it. On create, seed
    members from member_names if provided (otherwise the Artist starts with no
    Musicians). Does NOT change an existing artist's members — use
    set_artist_members.
    """
    name = (name or "").strip()
    artist = db.session.query(Artist).filter(
        func.lower(Artist.name) == name.lower()).first()
    if artist:
        return artist
    artist = Artist(name=name)
    db.session.add(artist)
    db.session.flush()
    if member_names:
        set_artist_members(artist, member_names)

    # MusicBrainz lookup on creation (2026-08-07). Synchronous, and deliberately
    # so: this runs inside the ingest BACKGROUND job, where the user is already
    # watching a copy progress bar, and doing it here means the facts are set on
    # the in-session object before the caller commits — no second thread, no
    # race against an uncommitted row.
    #
    # Cannot fail an ingest: try_match_artist() swallows everything, is a
    # no-op under TESTING, and a process-wide circuit breaker stops retrying
    # once offline (otherwise a 40-show bulk import would spend eight minutes
    # timing out on DNS). See app/utils/musicbrainz.py.
    from app.utils import musicbrainz as _mb
    _mb.try_match_artist(artist)

    return artist
