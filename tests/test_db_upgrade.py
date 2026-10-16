"""tests/test_db_upgrade.py -- Alembic is the migration system (app/utils/db_upgrade.py,
app/migrations/).

The suite's app fixture skips the upgrade (TESTING), so these tests build databases
themselves: a "fresh" one with create_all(), and "old" ones that are a fresh one with
columns and tables taken away, as a v0.2.0-era install would have them.
"""
import shutil
import sqlite3
import sys
import types
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations

import app.models  # noqa: F401  registers every model
from app.extensions import db as _db
from app.utils import db_upgrade as du

REPO = Path(__file__).resolve().parent.parent
SNAPSHOT = REPO / "db" / "_cowork.db"

# What the "old" database lacks: columns added after v0.2.0 and tables added after it.
DROP_COLUMNS = {
    "artist": ["confirmed_at", "abbreviation", "mbid"],            # mbid carries an index
    "recording": ["location", "completeness_checked_at", "recording_engineer_id",
                  "transfer_engineer_id", "mastering_engineer_id", "mb_release_id",
                  "source_tag", "etree_shnid", "is_published"],
    "venue": ["history"],
    "event": ["history"],
    "bulk_ingest_run": ["cleared_at", "mode"],
    "peer_invite": ["code"],
    "track": ["disc_number", "disc_track_number", "original_file_path"],
    "collection": ["system_key"],
    "user": ["display_name", "avatar_ext"],
    "recording_fingerprint": ["rel_path"],
    "membership": ["instrument"],
}
DROP_TABLES = ["artist_alias", "venue_alias", "engineer_alias", "engineer_image", "engineer",
               "lomax_proposal", "lomax_run", "lomax_easter_egg_seen", "download_job",
               "recording_image", "remote_favorite"]


def _engine(path):
    """A plain engine with foreign keys OFF (app/extensions.py turns them on for every
    engine, and these tests rebuild tables)."""
    eng = sa.create_engine(f"sqlite:///{path}")

    @sa.event.listens_for(eng, "connect")
    def _off(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")
    return eng


def make_fresh(path):
    eng = _engine(path)
    _db.metadata.create_all(eng)
    eng.dispose()
    return path


def drop_columns(conn, table, cols):
    op = Operations(MigrationContext.configure(conn))
    with op.batch_alter_table(table) as batch:
        for ix in sa.inspect(conn).get_indexes(table):      # batch keeps an index on a dropped column
            if set(ix["column_names"]) & set(cols):
                batch.drop_index(ix["name"])
        for c in cols:
            batch.drop_column(c)


def make_old(path):
    """A fresh database with DROP_COLUMNS and DROP_TABLES taken away."""
    make_fresh(path)
    eng = _engine(path)
    with eng.begin() as conn:
        for table, cols in DROP_COLUMNS.items():
            drop_columns(conn, table, cols)
        for t in DROP_TABLES:
            conn.exec_driver_sql(f"DROP TABLE {t}")
    eng.dispose()
    return path


def schema_of(path):
    """Tables, columns (type, nullable, pk), indexes, unique constraints and foreign keys.
    Foreign keys come from PRAGMA foreign_key_list, not from SQLAlchemy's reflection: the
    reflection does not parse the inline `REFERENCES ... ON DELETE` clause that ALTER TABLE
    ADD COLUMN leaves in a table's SQL and reports the ON DELETE as missing."""
    eng = _engine(path)
    out = {}
    try:
        insp = sa.inspect(eng)
        with eng.connect() as conn:
            for t in insp.get_table_names():
                if t == "alembic_version":
                    continue
                fks = {}
                for r in conn.exec_driver_sql(f"PRAGMA foreign_key_list({t})"):
                    fks.setdefault(r[0], []).append(r)
                out[t] = {
                    "cols": {c["name"]: (str(c["type"]), c["nullable"], bool(c["primary_key"]))
                             for c in insp.get_columns(t)},
                    "idx": {(i["name"], tuple(i["column_names"]), bool(i["unique"]))
                            for i in insp.get_indexes(t)},
                    "uniq": {tuple(u["column_names"]) for u in insp.get_unique_constraints(t)},
                    "fks": {(tuple(r[3] for r in rows), rows[0][2], tuple(r[4] for r in rows), rows[0][6])
                            for rows in fks.values()},
                }
    finally:
        eng.dispose()
    return out


def revision_of(path):
    return du.read_state(path)[1]


# -- the baseline reaches the same schema as create_all ------------------------------------------

def test_upgraded_old_database_matches_a_fresh_one(tmp_path):
    fresh = make_fresh(tmp_path / "fresh.db")
    old = make_old(tmp_path / "trellis.db")
    before = schema_of(old)
    assert "location" not in before["recording"]["cols"] and "engineer" not in before
    result = du.upgrade_database(old)
    assert result["action"] == "baselined" and result["from"] is None
    assert revision_of(old) == result["to"] == "0002_location_source_of_truth"
    # Column order is not compared: ADD COLUMN appends, create_all follows the model.
    assert schema_of(old) == schema_of(fresh)


def test_upgraded_database_has_no_autogenerate_differences(tmp_path):
    old = make_old(tmp_path / "trellis.db")
    du.upgrade_database(old)
    eng = _engine(old)
    with eng.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}),
                                 _db.metadata)
    eng.dispose()
    # Filtered, with the reason: add_fk / remove_fk come from the reflection limit described in
    # schema_of(): inline `REFERENCES ... ON DELETE SET NULL` columns look different to
    # SQLAlchemy than the table-level constraint create_all writes. The real constraints are
    # compared through PRAGMA in the test above. Nothing else is filtered.
    left = [d for d in diffs if d[0] not in ("add_fk", "remove_fk")]
    assert left == []


def test_baseline_is_idempotent_on_a_current_schema(tmp_path):
    """A database built with create_all() by an app version that had no stamp: the baseline changes nothing."""
    path = make_fresh(tmp_path / "trellis.db")
    before = schema_of(path)
    assert du.upgrade_database(path)["action"] == "baselined"
    assert schema_of(path) == before
    assert du.upgrade_database(path)["action"] == "current"


# -- what happens to the file ----------------------------------------------------------------------

def test_absent_and_empty_files_are_left_alone(tmp_path):
    missing = tmp_path / "nope" / "trellis.db"
    assert du.upgrade_database(missing)["action"] == "absent"
    assert not missing.exists() and not missing.parent.exists()
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert du.upgrade_database(empty)["action"] == "empty"
    assert empty.stat().st_size == 0
    assert du.upgrade_database(None)["action"] == "absent"


def test_new_database_is_stamped_and_never_migrated(tmp_path):
    path = make_fresh(tmp_path / "trellis.db")
    assert du.stamp_new_database(path) is True
    assert revision_of(path) == "0002_location_source_of_truth"
    assert du.stamp_new_database(path) is False          # already stamped
    assert du.upgrade_database(path)["action"] == "current"
    assert not (tmp_path / "backups").exists()           # nothing ran, so nothing was backed up


def test_database_from_a_newer_trellis_is_refused(tmp_path):
    path = make_fresh(tmp_path / "trellis.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
    con.execute("INSERT INTO alembic_version VALUES ('9999_from_the_future')")
    con.commit()
    con.close()
    with pytest.raises(du.DatabaseTooNewError) as e:
        du.upgrade_database(path)
    assert str(e.value) == du.MSG_TOO_NEW
    assert revision_of(path) == "9999_from_the_future" and not (tmp_path / "backups").exists()


def test_database_older_than_v020_is_refused_and_untouched(tmp_path):
    path = tmp_path / "trellis.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE performer (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE artist (id INTEGER PRIMARY KEY, name TEXT)")
    con.commit()
    con.close()
    with pytest.raises(du.UnsupportedDatabaseError) as e:
        du.upgrade_database(path)
    assert str(e.value) == du.MSG_TOO_OLD
    assert not (tmp_path / "backups").exists()
    tables, rev = du.read_state(path)
    assert tables == {"performer", "artist"} and rev is None


def test_missing_not_null_column_without_a_known_default_is_refused(tmp_path):
    path = make_fresh(tmp_path / "trellis.db")
    eng = _engine(path)
    with eng.begin() as conn:
        op = Operations(MigrationContext.configure(conn))
        with op.batch_alter_table("track") as batch:
            batch.drop_column("title")                   # NOT NULL, no default: a far older schema
    eng.dispose()
    with pytest.raises(du.UnsupportedDatabaseError):
        du.upgrade_database(path)
    assert revision_of(path) is None
    assert "title" not in schema_of(path)["track"]["cols"]      # rolled back, not half-done


# -- backup ---------------------------------------------------------------------------------------------

def test_upgrade_backs_up_first_with_the_online_backup_api(tmp_path):
    path = make_old(tmp_path / "trellis.db")
    con = sqlite3.connect(path)
    con.execute("INSERT INTO venue (name) VALUES ('The Fillmore')")
    con.commit()
    con.close()
    result = du.upgrade_database(path)
    backup = result["backup"]
    assert backup.parent == tmp_path / "backups"
    assert backup.name.startswith("trellis-pre-alembic-") and backup.suffix == ".db"
    b = sqlite3.connect(backup)
    assert b.execute("SELECT name FROM venue").fetchall() == [("The Fillmore",)]
    assert "location" not in {r[1] for r in b.execute("PRAGMA table_info(recording)")}   # the OLD schema
    assert b.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    b.close()


def test_backup_is_consistent_for_a_wal_database(tmp_path):
    path = make_old(tmp_path / "trellis.db")
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("INSERT INTO venue (name) VALUES ('Only in the WAL')")
    con.commit()
    assert (tmp_path / "trellis.db-wal").stat().st_size > 0      # the row is not in the main file yet
    backup = du.backup_database(path, "pre-alembic")
    b = sqlite3.connect(backup)
    assert b.execute("SELECT name FROM venue").fetchall() == [("Only in the WAL",)]
    b.close()
    con.close()


def test_prune_keeps_the_newest_five(tmp_path):
    import os
    for i in range(8):
        f = tmp_path / f"trellis-x-{i}.db"
        f.write_bytes(b"x")
        os.utime(f, (1000 + i, 1000 + i))
    (tmp_path / "unrelated.txt").write_text("keep")
    removed = du.prune_backups(tmp_path)
    assert sorted(p.name for p in removed) == ["trellis-x-0.db", "trellis-x-1.db", "trellis-x-2.db"]
    assert sorted(p.name for p in tmp_path.glob("trellis-*.db")) == [f"trellis-x-{i}.db" for i in range(3, 8)]
    assert (tmp_path / "unrelated.txt").exists()


def test_prune_failure_is_not_fatal(tmp_path, monkeypatch):
    for i in range(7):
        (tmp_path / f"trellis-x-{i}.db").write_bytes(b"x")

    def boom(self, *a, **k):
        raise OSError("Operation not permitted")
    monkeypatch.setattr(Path, "unlink", boom)
    assert du.prune_backups(tmp_path) == []             # nothing removed, nothing raised


def test_backup_failure_stops_the_upgrade(tmp_path, monkeypatch):
    path = make_old(tmp_path / "trellis.db")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(du, "backup_database", boom)
    with pytest.raises(du.DatabaseUpgradeError) as e:
        du.upgrade_database(path)
    assert str(e.value) == du.MSG_BACKUP_FAILED
    assert revision_of(path) is None


# -- later revisions: ordinary Alembic, behind-head upgrades, failure and rollback ------------------

@pytest.fixture()
def second_revision(tmp_path, monkeypatch):
    """A copy of app/migrations with a 0003 revision added; db_upgrade is pointed at it."""
    mig = tmp_path / "migrations"
    shutil.copytree(REPO / "app" / "migrations", mig, ignore=shutil.ignore_patterns("__pycache__"))

    def write(body):
        (mig / "versions" / "0003_test.py").write_text(
            "from alembic import op\nimport sqlalchemy as sa\n"
            "revision = '0003_test'\ndown_revision = '0002_location_source_of_truth'\nbranch_labels = None\ndepends_on = None\n"
            + body)
    monkeypatch.setattr(du, "migrations_dir", lambda: mig)
    return write


def test_database_behind_head_is_backed_up_and_upgraded(tmp_path, second_revision):
    path = make_fresh(tmp_path / "trellis.db")
    du.stamp_new_database(path)                                   # at 0001
    second_revision("def upgrade():\n    op.create_table('note', sa.Column('id', sa.Integer, primary_key=True))\n")
    result = du.upgrade_database(path)
    assert (result["action"], result["from"], result["to"]) == ("upgraded", "0002_location_source_of_truth", "0003_test")
    assert result["backup"].name.startswith("trellis-0002_location_source_of_truth-")
    assert "note" in schema_of(path) and revision_of(path) == "0003_test"
    assert du.upgrade_database(path)["action"] == "current"


def test_failing_revision_rolls_back_and_names_the_backup(tmp_path, second_revision):
    path = make_fresh(tmp_path / "trellis.db")
    du.stamp_new_database(path)
    second_revision(
        "def upgrade():\n"
        "    op.create_table('half_done', sa.Column('id', sa.Integer, primary_key=True))\n"
        "    op.add_column('venue', sa.Column('extra', sa.Text))\n"
        "    raise RuntimeError('boom')\n")
    with pytest.raises(du.DatabaseUpgradeError) as e:
        du.upgrade_database(path)
    backups = list((tmp_path / "backups").glob("trellis-0002_location_source_of_truth-*.db"))
    assert len(backups) == 1 and str(backups[0]) in str(e.value)
    after = schema_of(path)
    assert "half_done" not in after and "extra" not in after["venue"]["cols"]     # DDL rolled back
    assert revision_of(path) == "0002_location_source_of_truth"


def test_migrations_run_with_foreign_keys_off_so_a_table_rebuild_deletes_nothing(tmp_path, second_revision):
    path = make_fresh(tmp_path / "trellis.db")
    con = sqlite3.connect(path)
    con.execute("INSERT INTO artist (name, default_personnel_mode) VALUES ('A', 'inherit')")
    con.execute("INSERT INTO artist_image (artist_id, filename, ext) VALUES (1, 'f', '.jpg')")   # ON DELETE CASCADE
    con.commit()
    con.close()
    du.stamp_new_database(path)
    second_revision("def upgrade():\n"
                    "    assert op.get_bind().exec_driver_sql('PRAGMA foreign_keys').scalar() == 0\n"
                    "    with op.batch_alter_table('artist') as b:\n"
                    "        b.add_column(sa.Column('tmp_col', sa.Text))\n")
    du.upgrade_database(path)
    con = sqlite3.connect(path)
    assert con.execute("SELECT count(*) FROM artist_image").fetchone() == (1,)
    con.close()


def test_a_revision_that_works_on_the_old_schema_runs_in_batch_mode(tmp_path, second_revision):
    path = make_fresh(tmp_path / "trellis.db")
    du.stamp_new_database(path)
    second_revision("def upgrade():\n    with op.batch_alter_table('venue') as b:\n        b.drop_column('history')\n")
    du.upgrade_database(path)
    assert "history" not in schema_of(path)["venue"]["cols"]


# -- the data steps of the baseline --------------------------------------------------------------

def test_baseline_data_steps_run_only_for_what_was_added(tmp_path):
    path = make_fresh(tmp_path / "trellis.db")
    con = sqlite3.connect(path)
    con.execute("INSERT INTO artist (name, created_at, default_personnel_mode) VALUES ('Old Act', '2026-01-02 03:04:05', 'inherit')")
    con.execute("INSERT INTO performance (artist_id, start_year, personnel_mode) VALUES (1, 1980, 'inherit')")
    con.execute("INSERT INTO recording (performance_id, folder_path, is_complete, is_official) "
                "VALUES (1, 'a', 1, 0)")
    for n, s in ((1, "Disc 2"), (2, "Disc 2"), (3, "Set 1"), (4, None)):
        con.execute("INSERT INTO track (recording_id, track_number, title, file_path, set_number, is_official) "
                    "VALUES (1, ?, 't', 'f', ?, 0)", (n, s))
    con.commit()
    con.close()
    # Take away the columns the data steps belong to (rows survive the rebuild).
    eng = _engine(path)
    with eng.begin() as conn:
        for table, cols in (("artist", ["confirmed_at"]), ("track", ["disc_number", "disc_track_number"]),
                            ("collection", ["system_key"])):
            drop_columns(conn, table, cols)
    eng.dispose()
    du.upgrade_database(path)
    con = sqlite3.connect(path)
    assert con.execute("SELECT confirmed_at FROM artist").fetchone() == ("2026-01-02 03:04:05",)
    assert con.execute("SELECT name, system_key FROM collection").fetchall() == [("Full Library", "full_library")]
    rows = con.execute("SELECT track_number, disc_number, disc_track_number, set_number "
                       "FROM track ORDER BY track_number").fetchall()
    assert rows == [(1, 2, 1, None), (2, 2, 2, None), (3, None, None, "Set 1"), (4, None, None, None)]
    assert con.execute("SELECT value FROM node_setting WHERE key='file_handling_mode'").fetchone() == ("organize",)
    con.close()


def test_a_current_database_is_not_rewritten_by_the_baseline(tmp_path):
    path = make_fresh(tmp_path / "trellis.db")
    con = sqlite3.connect(path)
    con.execute("INSERT INTO artist (name, default_personnel_mode) VALUES ('New Act', 'inherit')")          # confirmed_at stays NULL
    con.commit()
    con.close()
    du.upgrade_database(path)
    con = sqlite3.connect(path)
    assert con.execute("SELECT confirmed_at FROM artist").fetchone() == (None,)
    assert con.execute("SELECT count(*) FROM collection").fetchone() == (0,)
    con.close()


def test_shared_studio_performance_is_split(tmp_path):
    path = make_fresh(tmp_path / "trellis.db")
    con = sqlite3.connect(path)
    con.execute("INSERT INTO artist (name, default_personnel_mode) VALUES ('A', 'inherit')")
    con.execute("INSERT INTO performance (artist_id, start_year, personnel_mode) VALUES (1, 1970, 'inherit')")
    for i in (1, 2, 3):
        con.execute("INSERT INTO recording (performance_id, folder_path, is_complete, is_official, kind) "
                    "VALUES (1, ?, 1, 0, 'studio')", (f"r{i}",))
    con.commit()
    con.close()
    du.upgrade_database(path)
    con = sqlite3.connect(path)
    perfs = [r[0] for r in con.execute("SELECT performance_id FROM recording ORDER BY id")]
    assert perfs[0] == 1 and len(set(perfs)) == 3
    assert con.execute("SELECT count(*) FROM performance").fetchone() == (3,)
    con.close()


# -- the app ------------------------------------------------------------------------------------------

def _server_mode_config(path):
    from config import Config

    class C(Config):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"
        SERVER_MODE = True              # registers only the share blueprint: no threads, no update check
        DEV_MODE = False
        SINGLE_USER_DESKTOP = False
        SECRET_KEY = "x" * 40
        TESTING = False
    return C


def test_create_app_upgrades_an_old_database_and_leaves_an_absent_one(tmp_path):
    from app import create_app
    absent = tmp_path / "absent" / "trellis.db"
    create_app(config_class=_server_mode_config(absent))
    assert not absent.exists()                                   # an upgrade never creates a database
    old = make_old(tmp_path / "trellis.db")
    create_app(config_class=_server_mode_config(old))
    assert revision_of(old) == "0002_location_source_of_truth"
    assert "engineer" in schema_of(old)


def test_create_app_refuses_a_database_from_a_newer_trellis(tmp_path):
    from app import create_app
    path = make_fresh(tmp_path / "trellis.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
    con.execute("INSERT INTO alembic_version VALUES ('9999_from_the_future')")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="newer version of Trellis"):
        create_app(config_class=_server_mode_config(path))


def test_testing_apps_do_not_run_the_upgrade(app, tmp_path):
    assert app.config["TESTING"] and revision_of_app(app) is None


def revision_of_app(app):
    with _db.engine.connect() as conn:
        names = sa.inspect(conn).get_table_names()
    return "stamped" if "alembic_version" in names else None


def test_first_run_setup_stamps_the_new_database(app, monkeypatch, tmp_path):
    sys.modules.setdefault("webview", types.ModuleType("webview"))
    import run
    from app import create_app
    from config import Config
    path = tmp_path / "db" / "trellis.db"

    class C(Config):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"
        TESTING = True
        DEV_MODE = False
    new_app = create_app(config_class=C)
    monkeypatch.setattr(run, "app", new_app)
    monkeypatch.setattr(Config, "DB_PATH", path)
    monkeypatch.setattr(run, "_maybe_start_bulk_ingest_from_marker", lambda: None)
    run.first_run_setup(create_default_user=False)
    assert path.exists() and revision_of(path) == "0002_location_source_of_truth"
    assert du.upgrade_database(path)["action"] == "current"


# -- the real database, read-only snapshot ----------------------------------------------------------------

@pytest.mark.skipif(not SNAPSHOT.exists(), reason="no db/_cowork.db snapshot")
def test_snapshot_of_the_real_database_upgrades_clean(tmp_path):
    size = SNAPSHOT.stat().st_size
    if size > 400 * 1024 * 1024:
        pytest.skip("snapshot over 400MB")
    if shutil.disk_usage(tmp_path).free < size * 4:
        pytest.skip("not enough free disk for a copy and its backup")
    dest = tmp_path / "trellis.db"
    src = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro&immutable=1", uri=True)
    dst = sqlite3.connect(dest)
    src.backup(dst)
    src.close()
    dst.close()
    counts_before = _row_counts(dest)
    result = du.upgrade_database(dest)
    assert result["action"] == "baselined" and revision_of(dest) == "0002_location_source_of_truth"
    counts_after = _row_counts(dest)
    assert {t: n for t, n in counts_after.items() if t in counts_before} == counts_before   # no row lost or added
    eng = _engine(dest)
    with eng.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}),
                                 _db.metadata)
    eng.dispose()
    assert [d for d in diffs if d[0] not in ("add_fk", "remove_fk")] == []     # see the filter note above
    fresh = make_fresh(tmp_path / "fresh.db")
    a, b = schema_of(dest), schema_of(fresh)
    assert set(a) == set(b)
    for t in b:
        assert a[t]["cols"] == b[t]["cols"], t
        assert a[t]["fks"] == b[t]["fks"], t
    assert du.upgrade_database(dest)["action"] == "current"


def _row_counts(path):
    con = sqlite3.connect(path)
    try:
        return {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        con.close()
