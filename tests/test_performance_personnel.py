"""
tests/test_performance_personnel.py — Per-Show Personnel, Phase 1 (2026-07-18).

Covers: stint date-bound resolution (union of intervals, coarse-date
normalization), inherit vs explicit mode, guest rows, the stint-safe write
path (a roster resync must not destroy dated stint history — the actual bug
being fixed), pruning of guest-only Musicians, and the musician-aggregation
guest_appearances field. Pure logic + DB behavior against the seeded temp DB
— no filesystem/FLAC/librosa needed. See "Context Library/Per-Show
Personnel — Design Plan (DRAFT).md".
"""

import pytest

from app.extensions import db as _db
from app.models.musician import Musician, Membership
from app.models.artist import Artist
from app.models.performance import Performance
from app.models.performance_personnel import PerformancePersonnel
from app.models.recording import Recording
from app.utils.artists import (
    resolve_or_create_musician, set_artist_members,
    add_membership_stint, update_membership_stint_bounds, remove_membership_stint,
)
from app.utils.personnel import resolve_performance_personnel
from app.utils.pruning import prune_after_recording_delete, _delete_orphan_musicians


@pytest.fixture()
def api(app):
    """A test client with auth disabled — exercises the JSON CRUD endpoints."""
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


def _make_artist(name):
    p = Artist(name=name)
    _db.session.add(p)
    _db.session.flush()
    return p


def _make_performance(artist, year, month=None, day=None, mode="inherit"):
    perf = Performance(artist_id=artist.id, start_year=year,
                       start_month=month, start_day=day, personnel_mode=mode)
    _db.session.add(perf)
    _db.session.flush()
    return perf


# ── Resolver: stint date-bound coverage ─────────────────────────────────────

def test_unbounded_membership_always_covers(app, seeded_ids):
    """Every pre-2026-07-18 row is NULL/NULL and must resolve identically to
    'always a member' — for a normal date, and for a performance with no
    date at all (never silently drop someone over missing data)."""
    artist = _db.session.get(Artist, seeded_ids["artist_id"])
    perf = _make_performance(artist, 1980, 2, 22)
    resolved = resolve_performance_personnel(perf)
    assert [r["name"] for r in resolved] == ["Bill Evans"]
    assert resolved[0]["source"] == "inherited"

    perf2 = _make_performance(artist, None)
    resolved2 = resolve_performance_personnel(perf2)
    assert [r["name"] for r in resolved2] == ["Bill Evans"]


def test_second_stint_mickey_hart(app):
    """Two dated stints for the same person: covered during either window,
    excluded in the gap between them."""
    artist = _make_artist("Grateful Dead")
    mickey = resolve_or_create_musician("Mickey Hart")
    _db.session.add(Membership(artist_id=artist.id, musician_id=mickey.id, order=0,
                               start_year=1967, end_year=1971, end_month=2))
    _db.session.add(Membership(artist_id=artist.id, musician_id=mickey.id, order=0,
                               start_year=1974, start_month=10, end_year=1995))
    _db.session.flush()

    during_first  = _make_performance(artist, 1969, 6, 1)
    in_the_gap    = _make_performance(artist, 1972, 6, 1)
    during_second = _make_performance(artist, 1980, 1, 1)

    assert "Mickey Hart" in [r["name"] for r in resolve_performance_personnel(during_first)]
    assert "Mickey Hart" not in [r["name"] for r in resolve_performance_personnel(in_the_gap)]
    assert "Mickey Hart" in [r["name"] for r in resolve_performance_personnel(during_second)]


def test_second_stint_dedupes_to_one_entry(app):
    """A person with 2+ covering stints appears exactly once, not once per
    stint row."""
    artist = _make_artist("Overlap Test Act")
    person = resolve_or_create_musician("Overlapping Person")
    _db.session.add(Membership(artist_id=artist.id, musician_id=person.id, order=0,
                               start_year=1970, end_year=1980))
    _db.session.add(Membership(artist_id=artist.id, musician_id=person.id, order=0,
                               start_year=1975, end_year=1985))   # overlaps the first
    _db.session.flush()
    perf = _make_performance(artist, 1978)
    resolved = resolve_performance_personnel(perf)
    assert len([r for r in resolved if r["name"] == "Overlapping Person"]) == 1


def test_coarse_end_date_rounds_permissively(app):
    """A stint ending (1971, 2, None) covers every day through 1971-02-28
    (last day of that month), not just 1971-02-01."""
    artist = _make_artist("Coarse Date Act")
    person = resolve_or_create_musician("Coarse Person")
    _db.session.add(Membership(artist_id=artist.id, musician_id=person.id, order=0,
                               end_year=1971, end_month=2))   # no end_day
    _db.session.flush()

    still_in_feb = _make_performance(artist, 1971, 2, 28)
    into_march   = _make_performance(artist, 1971, 3, 1)

    assert "Coarse Person" in [r["name"] for r in resolve_performance_personnel(still_in_feb)]
    assert "Coarse Person" not in [r["name"] for r in resolve_performance_personnel(into_march)]


# ── Resolver: explicit mode ignores the act roster ─────────────────────────

def test_explicit_mode_ignores_act_roster(app):
    artist = _make_artist("Acoustic All-Stars")
    bela = resolve_or_create_musician("Bela Fleck")
    _db.session.add(Membership(artist_id=artist.id, musician_id=bela.id, order=0))
    _db.session.flush()

    perf = _make_performance(artist, 1998, mode="explicit")
    # Bela is on the act roster, but explicit mode must not fall back to it.
    assert resolve_performance_personnel(perf) == []

    sam = resolve_or_create_musician("Sam Bush")
    _db.session.add(PerformancePersonnel(performance_id=perf.id, musician_id=sam.id, order=0))
    _db.session.flush()
    # perf.personnel was already accessed (and cached empty) by the resolve
    # call above; the new row was added via a raw FK insert, not through the
    # ORM relationship, so refresh before reading it again. Every real call
    # site (GET/PUT handlers) fetches a fresh Performance per request and
    # never hits this — this is purely about reusing one Python object
    # across a mutation within a single test.
    _db.session.refresh(perf)
    resolved = resolve_performance_personnel(perf)
    assert [r["name"] for r in resolved] == ["Sam Bush"]
    assert resolved[0]["source"] == "explicit"


def test_inherit_mode_layers_guests_onto_roster(app, seeded_ids):
    artist = _db.session.get(Artist, seeded_ids["artist_id"])
    perf = _make_performance(artist, 1980, 2, 22)
    guest = resolve_or_create_musician("Special Guest")
    _db.session.add(PerformancePersonnel(performance_id=perf.id, musician_id=guest.id,
                                         order=1, is_guest=True, instrument="sax"))
    _db.session.flush()
    resolved = resolve_performance_personnel(perf)
    by_name = {r["name"]: r["source"] for r in resolved}
    assert by_name == {"Bill Evans": "inherited", "Special Guest": "guest"}


# ── Stint-safe write path (Chunk 2's actual fix) ────────────────────────────

def test_set_artist_members_preserves_dated_stint(app):
    """Resyncing a roster with the same name present must not wipe a real
    stint's dates — the old delete-and-recreate behavior would have."""
    artist = _make_artist("Allman Brothers Band")
    duane = resolve_or_create_musician("Duane Allman")
    _db.session.add(Membership(artist_id=artist.id, musician_id=duane.id, order=0,
                               start_year=1969, end_year=1971, end_month=10, end_day=29))
    _db.session.flush()
    stint_id = _db.session.query(Membership).filter_by(
        artist_id=artist.id, musician_id=duane.id).first().id

    set_artist_members(artist, ["Duane Allman"])   # resync, same name

    m = _db.session.get(Membership, stint_id)
    assert m is not None, "resync deleted the stint row instead of preserving it"
    assert (m.start_year, m.end_year, m.end_month, m.end_day) == (1969, 1971, 10, 29)


def test_set_artist_members_keeps_dated_stint_when_dropped(app):
    """Dropping a name with real stint history from the list must NOT delete
    it outright — only a fully-unbounded single row is safe to remove."""
    artist = _make_artist("Departed Member Act")
    person = resolve_or_create_musician("Left The Band")
    _db.session.add(Membership(artist_id=artist.id, musician_id=person.id, order=0,
                               start_year=1970, end_year=1975))
    _db.session.flush()

    set_artist_members(artist, [])   # drop everyone

    still_there = _db.session.query(Membership).filter_by(
        artist_id=artist.id, musician_id=person.id).first()
    assert still_there is not None


def test_set_artist_members_drops_unbounded_row_when_removed(app, seeded_ids):
    """The common case (no stint dates) still behaves exactly like before
    2026-07-18: dropping a name from the list deletes their membership."""
    artist = _db.session.get(Artist, seeded_ids["artist_id"])
    set_artist_members(artist, [])
    remaining = _db.session.query(Membership).filter_by(artist_id=artist.id).count()
    assert remaining == 0


def test_add_membership_stint_does_not_disturb_existing_stints(app):
    artist = _make_artist("Warren Haynes Test Act")
    warren = resolve_or_create_musician("Warren Haynes")
    first = add_membership_stint(artist, "Warren Haynes", start_year=1989, end_year=1997)
    second = add_membership_stint(artist, "Warren Haynes", start_year=2001, end_year=2014)
    assert first.id != second.id
    stints = _db.session.query(Membership).filter_by(
        artist_id=artist.id, musician_id=warren.id).all()
    assert len(stints) == 2
    assert {s.start_year for s in stints} == {1989, 2001}


def test_update_and_remove_membership_stint(app):
    artist = _make_artist("Stint Edit Act")
    m = add_membership_stint(artist, "Someone", start_year=1980)
    update_membership_stint_bounds(m.id, start_year=1981, end_year=1985)
    updated = _db.session.get(Membership, m.id)
    assert (updated.start_year, updated.end_year) == (1981, 1985)

    assert remove_membership_stint(m.id) is True
    assert _db.session.get(Membership, m.id) is None
    assert remove_membership_stint(999999) is False


# ── API: PUT /api/performances/<id> members (the actual bug fix) ───────────

def test_recording_page_edit_does_not_touch_act_roster(api, seeded_ids):
    """The original bug: editing a recording page's Musicians pills used to
    call set_artist_members(p.artist, ...), rewriting the ACT's global
    roster. Adding a guest here must leave the act roster — and any OTHER
    performance of that act — untouched. Members/Guests two-row UI
    (2026-07-22): the two roles are now two separate submitted lists, not one
    list with guest-vs-member inferred by diffing."""
    perf_id = seeded_ids["performance_id"]
    artist_id = seeded_ids["artist_id"]

    other_perf = _make_performance(_db.session.get(Artist, artist_id), 1981, 3, 1)
    _db.session.commit()

    resp = api.put(f"/api/performances/{perf_id}",
                   json={"members": ["Bill Evans"], "guests": ["Sit-In Guest"]})
    assert resp.status_code == 200

    roster = _db.session.query(Membership).filter_by(artist_id=artist_id).all()
    assert len(roster) == 1
    assert roster[0].musician.name == "Bill Evans"

    other_resolved = resolve_performance_personnel(_db.session.get(Performance, other_perf.id))
    assert "Sit-In Guest" not in [r["name"] for r in other_resolved]

    this_perf = _db.session.get(Performance, perf_id)
    assert this_perf.personnel_mode == "inherit"
    resolved = resolve_performance_personnel(this_perf)
    by_name = {r["name"]: r["source"] for r in resolved}
    assert by_name == {"Bill Evans": "inherited", "Sit-In Guest": "guest"}
    is_guest_by_name = {r["name"]: r["is_guest"] for r in resolved}
    assert is_guest_by_name == {"Bill Evans": False, "Sit-In Guest": True}


def test_dropping_inherited_member_switches_to_explicit(api, seeded_ids):
    """Case 5: removing an act-roster member from one show's pill row can't
    edit their global membership dates away — it switches THIS performance
    to explicit mode with a snapshot lineup instead, and the act roster is
    unaffected."""
    perf_id = seeded_ids["performance_id"]

    resp = api.put(f"/api/performances/{perf_id}", json={"members": []})
    assert resp.status_code == 200

    perf = _db.session.get(Performance, perf_id)
    assert perf.personnel_mode == "explicit"
    assert resolve_performance_personnel(perf) == []

    membership = _db.session.query(Membership).filter_by(
        artist_id=seeded_ids["artist_id"]).first()
    assert membership is not None
    assert membership.musician.name == "Bill Evans"


def test_adding_member_in_inherit_mode_does_not_flip_to_explicit(api, seeded_ids):
    """Members/Guests two-row UI (2026-07-22): adding someone via the Members
    row who isn't on the act roster is a full-member addition for this show,
    not a guest sit-in — but it must NOT flip the performance to explicit
    mode (only DROPPING an inherited member does that, per case 5)."""
    perf_id = seeded_ids["performance_id"]

    resp = api.put(f"/api/performances/{perf_id}",
                   json={"members": ["Bill Evans", "Subbing In"], "guests": []})
    assert resp.status_code == 200

    perf = _db.session.get(Performance, perf_id)
    assert perf.personnel_mode == "inherit"   # unchanged — nobody was dropped
    resolved = resolve_performance_personnel(perf)
    by_name = {r["name"]: (r["source"], r["is_guest"]) for r in resolved}
    assert by_name == {"Bill Evans": ("inherited", False), "Subbing In": ("added", False)}


def test_added_member_later_joining_roster_does_not_duplicate(api, seeded_ids):
    """Regression test (Ryan, 2026-07-23 — JD Crowe & the New South, recording
    #239): add someone via the recording page's Members row (creates a
    performance_personnel 'added' row), then separately add that SAME person
    to the act's roster on the Artist page (a new Membership). Both
    mechanisms now cover them, but resolve_performance_personnel must show
    them exactly once — and removing that one pill must actually remove
    them, not silently no-op because a duplicate with the same name kept the
    name "in the target set" from the diffing's point of view."""
    perf_id = seeded_ids["performance_id"]
    artist = _db.session.get(Performance, perf_id).artist

    # Step 1: add three people via the recording page's Members row — none of
    # them are on the act roster yet, so these land as 'added' rows.
    resp = api.put(f"/api/performances/{perf_id}",
                   json={"members": ["Bill Evans", "Bobby Sloane", "Tony Rice", "Ricky Skaggs"],
                         "guests": []})
    assert resp.status_code == 200
    resolved = resolve_performance_personnel(_db.session.get(Performance, perf_id))
    assert len(resolved) == 4   # sanity check before the roster edit

    # Step 2: separately add those same three people to the ACT roster (the
    # Artist page's "+" button) — unbounded stints, so they cover every
    # date including this one.
    add_membership_stint(artist, "Bobby Sloane")
    add_membership_stint(artist, "Tony Rice")
    add_membership_stint(artist, "Ricky Skaggs")
    _db.session.commit()

    # Bug: without dedup, this would be 7 (4 + 3 redundant leftover rows).
    resolved = resolve_performance_personnel(_db.session.get(Performance, perf_id))
    names = [r["name"] for r in resolved]
    assert len(names) == len(set(names)) == 4
    assert set(names) == {"Bill Evans", "Bobby Sloane", "Tony Rice", "Ricky Skaggs"}
    # The surviving entry for each newly-rostered person must be the
    # 'inherited' one (not the stale 'added' row) — that's what makes
    # removing them go through the correct case-5 flow next.
    by_name = {r["name"]: r["source"] for r in resolved}
    assert by_name["Tony Rice"] == "inherited"

    # Step 3: click X on Tony Rice's (now single) pill — must actually
    # remove him, not silently no-op.
    resp = api.put(f"/api/performances/{perf_id}",
                   json={"members": ["Bill Evans", "Bobby Sloane", "Ricky Skaggs"],
                         "guests": []})
    assert resp.status_code == 200
    resolved = resolve_performance_personnel(_db.session.get(Performance, perf_id))
    names = {r["name"] for r in resolved}
    assert "Tony Rice" not in names
    assert names == {"Bill Evans", "Bobby Sloane", "Ricky Skaggs"}


def test_guests_list_independent_of_members_list(api, seeded_ids):
    """Omitting 'members' from the PUT payload must leave the Members row
    untouched while still reconciling 'guests' — the two rows are
    independently updatable, not one combined list."""
    perf_id = seeded_ids["performance_id"]

    api.put(f"/api/performances/{perf_id}", json={"guests": ["Guest One"]})
    resolved = resolve_performance_personnel(_db.session.get(Performance, perf_id))
    by_name = {r["name"]: r["is_guest"] for r in resolved}
    assert by_name == {"Bill Evans": False, "Guest One": True}

    # Add a second guest without resending 'members' — Bill Evans must survive.
    api.put(f"/api/performances/{perf_id}", json={"guests": ["Guest One", "Guest Two"]})
    resolved = resolve_performance_personnel(_db.session.get(Performance, perf_id))
    by_name = {r["name"]: r["is_guest"] for r in resolved}
    assert by_name == {"Bill Evans": False, "Guest One": True, "Guest Two": True}

    # Remove Guest One by resending guests without it — a plain row delete,
    # no mode flip (guests never trigger case 5).
    api.put(f"/api/performances/{perf_id}", json={"guests": ["Guest Two"]})
    perf = _db.session.get(Performance, perf_id)
    assert perf.personnel_mode == "inherit"
    resolved = resolve_performance_personnel(perf)
    assert {r["name"] for r in resolved} == {"Bill Evans", "Guest Two"}


def test_get_performance_reflects_resolved_personnel(api, seeded_ids):
    resp = api.get(f"/api/performances/{seeded_ids['performance_id']}")
    body = resp.get_json()
    assert body["personnel_mode"] == "inherit"
    assert [m["name"] for m in body["members"]] == ["Bill Evans"]
    assert body["personnel"][0]["source"] == "inherited"


# ── Pruning: guest-only Musicians survive while referenced ───────────────────

def test_guest_only_musician_survives_pruning_while_referenced(app):
    """An Musician with zero Memberships, referenced only by a
    performance_personnel row, must not be prunable while that row exists."""
    artist = _make_artist("Pruning Guest Act")
    perf = _make_performance(artist, 1990)
    guest = resolve_or_create_musician("Untouchable Guest")
    _db.session.add(PerformancePersonnel(performance_id=perf.id, musician_id=guest.id, order=0))
    _db.session.flush()

    deleted = _delete_orphan_musicians([guest.id])
    assert deleted == []
    assert _db.session.get(Musician, guest.id) is not None


def test_guest_only_musician_pruned_after_recording_delete(app):
    """Once the referencing recording (and its performance) is gone, a
    guest with no Membership anywhere is cleaned up rather than orphaned
    forever."""
    artist = _make_artist("Guest Cleanup Act")
    perf = _make_performance(artist, 1990)
    guest = resolve_or_create_musician("Ephemeral Guest")
    _db.session.add(PerformancePersonnel(performance_id=perf.id, musician_id=guest.id, order=0))
    rec = Recording(performance_id=perf.id, source="AUD", quality="B",
                    folder_path="x/y")
    _db.session.add(rec)
    _db.session.flush()

    _db.session.query(Recording).filter_by(id=rec.id).delete(synchronize_session=False)
    _db.session.flush()
    pruned = prune_after_recording_delete(perf.id)
    _db.session.commit()

    assert guest.id in pruned["musicians"]
    assert _db.session.get(Musician, guest.id) is None


# ── Musician aggregation: guest_appearances ───────────────────────────────────

def test_guest_appearance_surfaces_on_musician_page(api, app):
    """Covers the real bug Ryan hit: Darol Anger added as a guest via the
    recording page pill row didn't show up on his Musician page at all — the
    backend computed guest_appearances (Chunk 4) but the frontend never
    rendered them (2026-07-18 fix). This asserts the API includes everything
    the person-page UI needs to render a clickable recording row: split
    date parts, venue/location, and the actual recordings — not just a
    display string."""
    artist = _make_artist("Someone Else's Band")
    perf = _make_performance(artist, 1985, 7, 4)
    guest = resolve_or_create_musician("Cross Pollinator")
    _db.session.add(PerformancePersonnel(performance_id=perf.id, musician_id=guest.id,
                                         order=0, instrument="trumpet", is_guest=True))
    rec = Recording(performance_id=perf.id, source="SBD", quality="A", folder_path="x/y")
    _db.session.add(rec)
    _db.session.commit()

    resp = api.get(f"/api/musicians/{guest.id}")
    body = resp.get_json()
    assert body["artists"] == []   # not a Membership of anything
    assert len(body["guest_appearances"]) == 1
    ga = body["guest_appearances"][0]
    assert ga["artist_name"] == "Someone Else's Band"
    assert ga["instrument"] == "trumpet"
    assert ga["date"] == "1985-07-04"
    assert (ga["start_year"], ga["start_month"], ga["start_day"]) == (1985, 7, 4)
    assert len(ga["recordings"]) == 1
    assert ga["recordings"][0]["id"] == rec.id
    assert ga["recordings"][0]["source"] == "SBD"


def test_guest_appearance_not_duplicated_for_actual_members(api, seeded_ids):
    """A performance_personnel row for someone who's ALSO a formal member of
    that same act shouldn't double-list the act under guest_appearances —
    it's already covered by `artists`."""
    perf_id = seeded_ids["performance_id"]
    musician_id = seeded_ids["musician_id"]
    _db.session.add(PerformancePersonnel(performance_id=perf_id, musician_id=musician_id, order=5))
    _db.session.commit()

    resp = api.get(f"/api/musicians/{musician_id}")
    body = resp.get_json()
    assert any(p["id"] == seeded_ids["artist_id"] for p in body["artists"])
    assert body["guest_appearances"] == []
