"""
tests/test_sqlite_wal.py — the database must be in WAL mode, and stay there.

WHY THIS FILE EXISTS
--------------------
Once the share node runs alongside the desktop app against the same database
file, two processes are reading and writing it. SQLite's DEFAULT rollback
journal writes new data into the main file, so a writer must lock readers out
— and a peer streaming a FLAC is a long-lived reader. An edit in the desktop
app mid-stream becomes "database is locked", which reaches the peer as a
failed request that looks exactly like the tunnel dropping.

WAL removes that contention (new data goes to a side log; the main file is
never written mid-read). It is set in app/extensions.py on connect.

The failure mode this guards against is SILENCE. journal_mode is persisted in
the file, so a database that quietly comes up in `delete` mode is not an error
anywhere — it is just slow and occasionally broken, somewhere else, later.
"""

from sqlalchemy import text

from app.extensions import db


def _pragma(name):
    return db.session.execute(text(f"PRAGMA {name}")).scalar()


def test_journal_mode_is_wal(app):
    """The whole point. A file-backed database must be in WAL."""
    assert _pragma("journal_mode").lower() == "wal"


def test_foreign_keys_still_on(app):
    """
    Negative control for the edit that ADDED WAL.

    foreign_keys and journal_mode are now set by the same connect handler. A
    patch that broke the handler, or returned early before reaching the second
    pragma, would leave one of these wrong. Asserting both means a handler that
    silently stopped running cannot pass this file.

    (foreign_keys is NOT persisted in the file — it is off by default and must
    be re-issued per connection — so this genuinely exercises the handler
    rather than reading back a property of the file.)
    """
    assert _pragma("foreign_keys") == 1


def test_busy_timeout_is_generous(app):
    """
    WAL fixes reader/writer contention, NOT writer/writer — two writers still
    serialise. config.py passes connect_args timeout=60 so the loser WAITS
    instead of failing immediately. Asserted here because that timeout and WAL
    are two halves of one decision, and removing either alone looks harmless.

    SQLite reports busy_timeout in milliseconds.
    """
    assert _pragma("busy_timeout") >= 60_000
