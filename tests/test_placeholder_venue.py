"""tests/test_placeholder_venue.py — placeholder venue names never become a
shared, canonical Venue row.

app/utils/venues.py's is_placeholder_venue_name() is the single detector used
by _do_confirm (backend) and the mirrored PLACEHOLDER_VENUE_NAMES/
isPlaceholderVenue in app.js (frontend — not exercised by these Python
tests, see the JS comment blocks at the venue/city/state/country cases in
applyRecProposal and lockLocation).
"""

import pytest

from app.utils.venues import is_placeholder_venue_name, PLACEHOLDER_VENUE_NAMES
from app.models.recording import Recording
from app.models.performance import Performance
from app.models.venue import Venue


# ── Pure detector ────────────────────────────────────────────────────────────

def test_is_placeholder_venue_name_matches_known_stand_ins():
    for name in ["Unknown Venue", "unknown venue", "  Unknown Venue  ",
                 "Unknown", "TBD", "tbd", "N/A", "Various"]:
        assert is_placeholder_venue_name(name), name


def test_is_placeholder_venue_name_leaves_real_venues_alone():
    for name in ["The Fillmore", "Sprague Memorial Hall", "Red Rocks Amphitheatre"]:
        assert not is_placeholder_venue_name(name)


def test_is_placeholder_venue_name_handles_none_and_empty():
    assert not is_placeholder_venue_name(None)
    assert not is_placeholder_venue_name("")
    assert not is_placeholder_venue_name("   ")


# ── _do_confirm integration ──────────────────────────────────────────────────

def test_do_confirm_placeholder_venue_name_does_not_link_or_create_venue(app, db, tmp_path):
    """A show ingested with venue_name='Unknown Venue' must NOT create/reuse a
    Venue row — city/state/country should land on the Performance's own
    fallback fields instead, same as if no venue name had been given at all."""
    from app.api.ingest import _do_confirm
    from app.models.user import User

    src = tmp_path / "src_show"; src.mkdir()
    (src / "t01.flac").write_bytes(b"x" * 2000)
    lib = tmp_path / "lib"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    data = {
        "source_folder_path": str(src),
        "artist_name": "Placeholder Venue Test Act",
        "start_year": 1996, "start_month": 11, "start_day": 12,
        "venue_name": "Unknown Venue",
        "city": "Denver", "state": "CO", "country": "USA",
        "source": "AUD",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}],
    }
    result = _do_confirm(data, uid, None)
    rec = db.session.get(Recording, result["recording_id"])
    perf = db.session.get(Performance, rec.performance_id)

    assert perf.venue_id is None
    assert perf.city == "Denver" and perf.state == "CO" and perf.country == "USA"
    assert not db.session.query(Venue).filter(
        db.func.lower(Venue.name) == "unknown venue"
    ).first()


def test_do_confirm_two_placeholder_shows_stay_independent(app, db, tmp_path):
    """The exact contamination scenario from the user's bug report: two different
    shows both marked 'Unknown Venue' must NOT end up sharing one Venue row
    (and therefore can't clobber each other's location).
    """
    from app.api.ingest import _do_confirm
    from app.models.user import User

    lib = tmp_path / "lib"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    def _confirm(name, year, month, day, city, state):
        src = tmp_path / f"src_{name}_{year}"; src.mkdir()
        (src / "t01.flac").write_bytes(b"x" * 2000)
        data = {
            "source_folder_path": str(src),
            "artist_name": name,
            "start_year": year, "start_month": month, "start_day": day,
            "venue_name": "Unknown Venue",
            "city": city, "state": state, "country": "USA",
            "source": "AUD",
            "tracks": [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}],
        }
        return _do_confirm(data, uid, None)

    r1 = _confirm("CSNY", 1969, 12, 14, "Unknown City A", "XX")
    r2 = _confirm("Al Di Meola Trio", 1996, 11, 12, "Denver", "CO")

    rec1 = db.session.get(Recording, r1["recording_id"])
    rec2 = db.session.get(Recording, r2["recording_id"])
    p1 = db.session.get(Performance, rec1.performance_id)
    p2 = db.session.get(Performance, rec2.performance_id)

    assert p1.venue_id is None and p2.venue_id is None
    assert p1.city == "Unknown City A" and p2.city == "Denver"
    assert not db.session.query(Venue).filter(
        db.func.lower(Venue.name) == "unknown venue"
    ).first()
