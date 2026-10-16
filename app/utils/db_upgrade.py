"""app/utils/db_upgrade.py -- the startup sequence for the library database (Alembic).

Called by create_app (skipped when TESTING) and, for a brand-new database, by
run.first_run_setup. The schema is changed ONLY by Alembic revisions
(app/migrations/versions/); see the developer note in app/migrations/env.py.

What happens to the database file at start:

  absent               nothing. first_run_setup() creates it with create_all() and
                       calls stamp_new_database(), so a new library starts at head and
                       no revision ever runs on it. An upgrade never creates a file.
  present, no tables   nothing (same as absent: something else will build it).
  no alembic_version   a database from before Alembic. Refuse it if it predates
                       v0.2.0 (UnsupportedDatabaseError); otherwise back it up, run
                       0001_baseline and everything after it.
  behind head          back it up (backup_database), then upgrade to head.
  at head              nothing.
  unknown revision     written by a newer Trellis: refuse (DatabaseTooNewError).

Everything runs in one SQLite transaction, so a revision that fails leaves the
database as it was. The backup is a SQLite online backup (safe in WAL mode), kept in
<folder of the database>/backups/, newest BACKUPS_KEPT of them.

Failures are raised as DatabaseUpgradeError (a RuntimeError). Their messages are
shown to the person, so they are product copy: keep them short and plain.
"""
import contextlib
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

BACKUPS_KEPT = 5
_BUSY_TIMEOUT = 60           # seconds a locked database is waited for

MSG_TOO_NEW = ("This library was updated by a newer version of Trellis. "
               "Install the newest Trellis to open it. Nothing was changed.")
MSG_TOO_OLD = ("This library was made by a version of Trellis older than 0.2.0, "
               "which can no longer be updated. Nothing was changed.")
MSG_BACKUP_FAILED = ("Trellis could not back up your library before updating it, "
                     "so it did not update. Nothing was changed.")
MSG_FAILED = ("Trellis could not finish updating your library. "
              "A backup from before the update is at {backup}")


class DatabaseUpgradeError(RuntimeError):
    """The library database could not be brought up to this version. str(e) is shown to the person."""


class DatabaseTooNewError(DatabaseUpgradeError):
    pass


class UnsupportedDatabaseError(DatabaseUpgradeError):
    pass


# -- Paths and configuration -------------------------------------------------

def sqlite_path_from_uri(uri):
    """File path of a sqlite URL, or None for anything else (other database, :memory:)."""
    from sqlalchemy.engine import make_url
    url = make_url(str(uri))
    if not url.drivername.startswith("sqlite"):
        return None
    db = url.database
    return None if (not db or db == ":memory:") else db


def migrations_dir():
    """app/migrations, from the source tree or from inside the PyInstaller bundle."""
    from config import resource_dir
    return Path(resource_dir()) / "app" / "migrations"


def alembic_config(db_path):
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir()))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + str(db_path).replace("%", "%%"))
    return cfg


def migration_engine(db_path):
    """An engine for running revisions. Its own connection, not the app's.

    pysqlite does not start a transaction before DDL, so by default a failed revision
    leaves half its tables behind. With isolation_level=None and an explicit BEGIN, DDL is
    transactional (SQLite supports that) and the whole upgrade commits or rolls back as one.
    Foreign keys are OFF for the same reason (see below).
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import NullPool
    engine = create_engine(f"sqlite:///{db_path}", poolclass=NullPool,
                           connect_args={"timeout": _BUSY_TIMEOUT, "isolation_level": None})

    # app/extensions.py turns foreign keys ON for every SQLite connection in the process.
    # Not here: dropping the old table of a batch rebuild would fire its ON DELETE CASCADE
    # actions and delete the rows of every child table. (SQLite ignores this pragma inside
    # a transaction, so it is set on connect, before BEGIN.) Runs after the global listener.
    @event.listens_for(engine, "connect")
    def _foreign_keys_off(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


@contextlib.contextmanager
def _no_bytecode():
    """Alembic imports the revision files from disk. Inside the installed app those sit in
    the signed bundle; do not let Python write __pycache__ into it."""
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = old


def head_and_known(cfg):
    """(head revision, set of every revision id this app ships)."""
    from alembic.script import ScriptDirectory
    with _no_bytecode():
        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        if len(heads) != 1:
            raise DatabaseUpgradeError(f"The migrations have {len(heads)} heads; there must be one.")
        return heads[0], {s.revision for s in script.walk_revisions()}


# -- Reading the state of the file ---------------------------------------------

def _connect(path):
    # mode=rw can never create a file: a path that vanished since the check is an error.
    return sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=_BUSY_TIMEOUT)


def read_state(path):
    """(table names, stored revision or None). The revision is None when there is no
    alembic_version table or it has no row."""
    con = _connect(path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rev = None
        if "alembic_version" in tables:
            row = con.execute("SELECT version_num FROM alembic_version").fetchone()
            rev = row[0] if row else None
        return tables, rev
    finally:
        con.close()


def unsupported_reason(tables):
    """Why a database that has no alembic_version cannot be upgraded, or None.

    v0.2.0 is the floor. The Performer to Artist and Artist to Musician renames (2026-09-09)
    predate it, and renaming is not something the baseline does."""
    if tables & {"performer", "canonical_artist", "artist_canonical", "performer_image", "performer_resource"}:
        return "old-names"
    if "artist" in tables and "musician" not in tables:
        return "old-names"
    return None


# -- Backups ---------------------------------------------------------------------

def backup_database(db_path, label, now=None):
    """Copy the database into <its folder>/backups/ with SQLite's online backup API
    (a plain file copy of a WAL database can be torn). Returns the new file's Path."""
    db_path = Path(db_path)
    dest_dir = db_path.parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(label)) or "unknown"
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S")
    dest = dest_dir / f"trellis-{label}-{stamp}.db"
    n = 1
    while dest.exists():                    # two backups in the same second
        n += 1
        dest = dest_dir / f"trellis-{label}-{stamp}-{n}.db"
    src = _connect(db_path)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    prune_backups(dest_dir)
    return dest


def prune_backups(backup_dir, keep=BACKUPS_KEPT):
    """Delete all but the newest `keep` trellis-*.db files. Never raises: an old backup
    that cannot be removed is not a reason to stop the app."""
    try:
        files = sorted(Path(backup_dir).glob("trellis-*.db"),
                       key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    except OSError as e:
        log.warning("db_upgrade: could not list backups in %s: %s", backup_dir, e)
        return []
    removed = []
    for old in files[keep:]:
        try:
            old.unlink()
            removed.append(old)
        except OSError as e:
            log.warning("db_upgrade: could not remove old backup %s: %s", old, e)
    return removed


# -- The sequence --------------------------------------------------------------------

def upgrade_database(db_path):
    """Bring the database file at db_path to the current schema. Returns a dict:
    {"action": absent|empty|current|upgraded|baselined, "from": rev|None, "to": rev|None,
     "backup": Path|None}. Raises DatabaseUpgradeError (or a subclass) on failure."""
    result = {"action": None, "from": None, "to": None, "backup": None}
    if db_path is None or not Path(db_path).is_file():
        result["action"] = "absent"
        return result
    try:
        tables, rev = read_state(db_path)
    except sqlite3.DatabaseError as e:
        raise DatabaseUpgradeError(f"Trellis could not open your library: {e}") from e
    tables.discard("sqlite_sequence")
    if not tables:
        result["action"] = "empty"
        return result

    cfg = alembic_config(db_path)
    head, known = head_and_known(cfg)
    result["to"] = head
    result["from"] = rev

    if rev is not None:
        if rev not in known:
            raise DatabaseTooNewError(MSG_TOO_NEW)
        if rev == head:
            result["action"] = "current"
            return result
        result["action"] = "upgraded"
    else:
        if unsupported_reason(tables):
            raise UnsupportedDatabaseError(MSG_TOO_OLD)
        result["action"] = "baselined"

    try:
        result["backup"] = backup_database(db_path, rev or "pre-alembic")
    except (OSError, sqlite3.Error) as e:
        log.error("db_upgrade: backup of %s failed: %s", db_path, e)
        raise DatabaseUpgradeError(MSG_BACKUP_FAILED) from e

    _run(cfg, db_path, "upgrade", head, result["backup"])
    log.warning("db_upgrade: %s %s from %s to %s (backup %s)", db_path, result["action"],
                rev or "pre-alembic", head, result["backup"])
    return result


def stamp_new_database(db_path):
    """Mark a database just built by create_all() as being at head, without running any
    revision. Does nothing if it already carries a revision. Never raises: if the stamp
    is lost the next start treats the database as pre-Alembic and runs the baseline,
    which changes nothing on a database that is already current."""
    try:
        _tables, rev = read_state(db_path)
        if rev is not None:
            return False
        cfg = alembic_config(db_path)
        head, _known = head_and_known(cfg)
        _run(cfg, db_path, "stamp", head, None)
        return True
    except Exception as e:                   # noqa: BLE001
        log.error("db_upgrade: could not stamp %s: %s", db_path, e)
        return False


def _run(cfg, db_path, what, head, backup):
    from alembic import command
    engine = migration_engine(db_path)
    try:
        with _no_bytecode(), engine.begin() as connection:
            cfg.attributes["connection"] = connection
            if what == "stamp":
                command.stamp(cfg, head)
            else:
                command.upgrade(cfg, head)
    except UnsupportedDatabaseError:
        raise
    except Exception as e:                   # noqa: BLE001
        log.exception("db_upgrade: %s of %s failed", what, db_path)
        if what == "stamp":
            raise
        raise DatabaseUpgradeError(MSG_FAILED.format(backup=backup)) from e
    finally:
        cfg.attributes.pop("connection", None)
        engine.dispose()


def upgrade_for_app(app):
    """create_app's entry point: upgrade the database the app is configured for."""
    path = sqlite_path_from_uri(app.config.get("SQLALCHEMY_DATABASE_URI", ""))
    return upgrade_database(path)
