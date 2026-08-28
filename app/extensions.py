"""
extensions.py — Shared Flask extension instances.

Instantiated here (without an app) so models can import `db`
without causing circular imports. The app factory calls db.init_app(app).
"""

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from sqlalchemy import event
from sqlalchemy.engine import Engine

db           = SQLAlchemy()
login_manager = LoginManager()


# Per-connection SQLite settings. This app is SQLite-only; the module check on
# the raw connection keeps this correct if that ever stops being true.
@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):
    if not type(dbapi_connection).__module__.startswith("sqlite3"):
        return

    cursor = dbapi_connection.cursor()

    # SQLite ignores declared foreign keys unless PRAGMA foreign_keys=ON is
    # issued on every connection — it is not persisted in the DB file, and is
    # OFF by default.
    cursor.execute("PRAGMA foreign_keys=ON")

    # Write-Ahead Logging (2026-08-27). REQUIRED once a second process shares
    # this database — the share node serving peers runs alongside the desktop
    # app, against the same file.
    #
    # In SQLite's default rollback-journal mode, a writer copies the OLD pages
    # aside and writes the NEW data into the main database file. The main file
    # is therefore mid-surgery while a write is in flight, so SQLite must lock
    # every reader out. A peer streaming a FLAC holds a read open for the whole
    # range request; an edit in the desktop app during that window is a
    # "database is locked" — and it surfaces to the peer as a failed request
    # indistinguishable from the tunnel dropping.
    #
    # WAL inverts it: NEW data is appended to a side log and the main file is
    # left untouched, so readers are never reading a file anyone is writing.
    # Readers and a writer stop blocking each other entirely. There is still
    # only ONE writer at a time — that half is covered by the 60s busy timeout
    # in SQLALCHEMY_ENGINE_OPTIONS (config.py), which makes a second writer
    # WAIT rather than fail.
    #
    # ⚠ WAL coordinates processes through a shared-memory file and does NOT
    # work over SMB/NFS. The library database must stay on local disk — it
    # lives in Application Support precisely so it does; it must never be moved
    # onto /Volumes/music.
    #
    # journal_mode is PERSISTED in the database file, so this is a no-op after
    # the first connection. Re-issuing it every time is deliberate: it costs
    # nothing and makes the setting self-healing if a file arrives in another
    # mode (a restored backup, a copy made by an older build).
    mode = cursor.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower()

    # Never report a failure as a different failure. If WAL could not be set,
    # concurrent access is quietly back to blocking and the symptom will show
    # up somewhere else entirely — usually as a remote read that "just failed".
    # "memory" is the legitimate exception: in-memory databases have no journal
    # and no second process to share with.
    if mode not in ("wal", "memory"):
        print(f"  ⚠ SQLite journal_mode is '{mode}', not WAL — concurrent "
              f"readers and writers WILL block each other. Is the database "
              f"on a network volume?", flush=True)

    cursor.close()
