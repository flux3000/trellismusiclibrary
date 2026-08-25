"""
tests/conftest.py — pytest fixtures.

Spins up the app against a throwaway temp SQLite DB, creates the schema, and
seeds a minimal but complete object graph (user → canonical artist → performer
→ venue → performance → recording → tracks) that the tests build on.

These tests cover pure logic and DB behavior only — no FLAC files, no librosa,
no filesystem library — so they run anywhere.
"""

import os
import tempfile
import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from app import create_app
from app.extensions import db as _db
from app.models.user import User
from app.models.performer import Performer
from app.models.artist import Artist, Membership
from app.models.venue import Venue
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.track import Track
from app.models.track_analysis import TrackAnalysis
from app.models.play_log import PlayLog


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    class TestConfig(Config):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"
        TESTING = True
        DEV_MODE = False

    application = create_app(config_class=TestConfig)

    # ── Per-request identity, as in production (2026-08-08) ──────────────────
    # This fixture holds ONE app context open for the whole test. Flask's
    # RequestContext.push() reuses an already-pushed app context for the same
    # app rather than creating a new one, so `g` — which lives on the app
    # context — is shared by every request the test client makes.
    #
    # Flask-Login caches the resolved user at g._login_user and skips
    # _load_user() when it is already set. So the FIRST request in a test
    # decides current_user for all the rest: an opening unauthenticated call
    # pins `anonymous`, and a later session login silently has no effect.
    #
    # That is how test_admin_peer_management_requires_admin ran for weeks
    # without reaching the role gate it exists to test. Production is
    # unaffected — a real request gets its own app context and a fresh `g`.
    #
    # Clearing the cache per request restores that invariant here.
    @application.before_request
    def _reset_flask_login_cache():
        from flask import g
        g.pop("_login_user", None)

    with application.app_context():
        _db.create_all()
        _seed()
        yield application
        _db.session.remove()
        _db.drop_all()
    os.unlink(path)


@pytest.fixture()
def db(app):
    return _db


def _seed():
    user = User(username="admin", role="admin", is_active=True, password_hash="x")
    _db.session.add(user)

    # Performer (act) + its sole member Artist (person)
    performer = Performer(name="Bill Evans", sort_name="Evans, Bill")
    _db.session.add(performer)
    _db.session.flush()
    person = Artist(name="Bill Evans")
    _db.session.add(person)
    _db.session.flush()
    _db.session.add(Membership(performer_id=performer.id, artist_id=person.id, order=0))

    venue = Venue(name="Sprague Memorial Hall", city="New Haven", state="CT", country="US")
    _db.session.add(venue)
    _db.session.flush()

    performance = Performance(performer_id=performer.id, venue_id=venue.id,
                              start_year=1980, start_month=2, start_day=22)
    _db.session.add(performance)
    _db.session.flush()

    rec = Recording(performance_id=performance.id, source="AUD", quality="B+",
                    is_complete=True, is_official=False, folder_path="Bill Evans/1980")
    _db.session.add(rec)
    _db.session.flush()

    t1 = Track(recording_id=rec.id, track_number=1, title="My Foolish Heart",
               duration=300, file_path="01.flac")
    t2 = Track(recording_id=rec.id, track_number=2, title="tuning",
               duration=60, file_path="02.flac", flags='["tuning"]')
    _db.session.add_all([t1, t2])
    _db.session.flush()

    # analysis + play_log on t1 — used to prove cascade cleanup
    _db.session.add(TrackAnalysis(track_id=t1.id, rms_db=-18.0))
    _db.session.add(PlayLog(user_id=user.id, track_id=t1.id, completed=True))
    _db.session.commit()


@pytest.fixture()
def seeded_ids(app):
    """Convenience IDs for the seeded graph."""
    performer = _db.session.query(Performer).filter_by(name="Bill Evans").first()
    person    = _db.session.query(Artist).filter_by(name="Bill Evans").first()
    rec       = _db.session.query(Recording).first()
    return {
        "performer_id":   performer.id,
        "artist_id":      person.id,
        "performance_id": rec.performance_id,
        "recording_id":   rec.id,
    }


# ── Rate-limiter isolation (2026-08-25) ──────────────────────────────────────
# app/utils/rate_limit.py keeps its counters in a module-level dict, which
# outlives any single test. Without this, enroll attempts accumulate across
# files and the eleventh one in a run starts failing for reasons that have
# nothing to do with what it is testing — the same shape of bug as the cached
# identity above, and just as confusing to chase.
@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app.utils import rate_limit
    rate_limit.reset()
    yield
    rate_limit.reset()
