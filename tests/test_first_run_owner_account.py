"""
tests/test_first_run_owner_account.py — the name typed on the setup page.

Ryan set up a second install on 2026-08-28, typed "jeff" into "What should we
call you?", and got `ryanfbaker` — his macOS account name. The page had asked
a question and thrown the answer away: `_create_owner_account()` opened with
`if db.session.query(User).first() is not None: return`, and a `trellis.db`
left in Application Support from an earlier run still held the first account
that machine ever made. Deleting the library folder does not reset an install.

`run.py` builds a Flask app at import time and opens a PyWebView window at
`__main__`. Neither is wanted here: `webview` is stubbed so the import works
on a headless box, and `run.app` is repointed at the throwaway test app so
every write in these tests lands in the temp database, never a real one.
"""

import sys
import types

import pytest

from app.extensions import db as _db
from app.models.play_log import PlayLog
from app.models.recording_event import RecordingEvent
from app.models.user import User, UserArtistPermission
from app.models.user_preference import UserPreference


def _empty_the_user_table():
    """
    A machine that has never had an account. Four tables carry a NOT NULL
    foreign key to `user`, and the fixture's admin owns rows in some of them
    — deleting the parent alone raises IntegrityError, which reads like a
    broken test rather than the constraint doing its job.
    """
    for model in (PlayLog, RecordingEvent, UserArtistPermission, UserPreference):
        _db.session.query(model).delete()
    _db.session.query(User).delete()
    _db.session.commit()


@pytest.fixture()
def api(app, monkeypatch):
    """FluxAPI, wired to the test app rather than the one run.py built."""
    sys.modules.setdefault("webview", types.ModuleType("webview"))
    import run
    monkeypatch.setattr(run, "app", app)
    return run.FluxAPI()


# ── A machine with no account yet ────────────────────────────────────────────

def test_the_typed_name_becomes_the_account(api, app):
    _empty_the_user_table()

    api._create_owner_account("jeff")

    assert _db.session.query(User).filter_by(username="jeff").first() is not None


def test_no_name_typed_still_leaves_a_usable_owner(api, app):
    """The field is required in the page's JS, so this is the belt to that
    braces — an account with no name at all can never sign in."""
    _empty_the_user_table()

    api._create_owner_account("")

    assert _db.session.query(User).first().username == "owner"


# ── A machine whose database survived an earlier install ─────────────────────

def test_the_typed_name_is_adopted_by_the_existing_account(api, app):
    """The reported bug, in one assertion."""
    existing = _db.session.query(User).first()
    assert existing.username == "admin", "fixture precondition"

    api._create_owner_account("jeff")

    assert _db.session.query(User).filter_by(username="jeff").first() is not None
    assert _db.session.query(User).count() == 1, "adopt the account, never add a second"


def test_an_empty_name_never_blanks_an_existing_one(api, app):
    api._create_owner_account("")
    assert _db.session.query(User).first().username == "admin"


def test_adopting_the_same_name_twice_is_harmless(api, app):
    """A retried submission — folders created, this step failed once."""
    api._create_owner_account("jeff")
    api._create_owner_account("jeff")
    assert _db.session.query(User).count() == 1
    assert _db.session.query(User).first().username == "jeff"


def test_a_name_another_account_holds_is_raised_not_swallowed(api, app):
    """
    confirm_trellis_root() turns this into a visible message on the setup
    page. Returning quietly here would be a second silent fallback, which is
    the entire fault being fixed.
    """
    _db.session.add(User(username="jeff", password_hash="x", role="user", is_active=True))
    _db.session.commit()

    with pytest.raises(ValueError):
        api._create_owner_account("jeff")

    assert _db.session.query(User).filter_by(username="admin").first() is not None
