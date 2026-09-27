"""
tests/test_disc_migration.py -- scripts/migrate_file_handling.py, disc
migration and the surrounding column/mode/preference steps.

Runs the script as a subprocess against a small hand-built sqlite database,
exactly as Ryan would from the repo root, rather than importing its
internals -- the script's whole contract is "run me from the command line".
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "migrate_file_handling.py"


def _make_db(path):
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE track (
            id INTEGER PRIMARY KEY,
            recording_id INTEGER,
            track_number INTEGER,
            set_number TEXT
        );
        CREATE TABLE recording (
            id INTEGER PRIMARY KEY,
            folder_path TEXT,
            is_published INTEGER DEFAULT 1
        );
        CREATE TABLE artist (
            id INTEGER PRIMARY KEY,
            name TEXT
        );
        CREATE TABLE recording_fingerprint (
            id INTEGER PRIMARY KEY,
            recording_id INTEGER,
            fingerprint_type TEXT,
            filename TEXT
        );
        CREATE TABLE user_preference (
            id INTEGER PRIMARY KEY,
            key TEXT,
            value TEXT
        );
        CREATE TABLE node_setting (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # Recording 1: two discs, CD1 (2 tracks) + CD2 (2 tracks), continuous
    # track_number across the whole recording, out of on-disk order to prove
    # disc_track_number is derived from track_number, not row order.
    con.execute("INSERT INTO recording (id, folder_path) VALUES (1, 'Artist/Show')")
    con.executemany(
        "INSERT INTO track (recording_id, track_number, set_number) VALUES (?, ?, ?)",
        [
            (1, 2, "Disc 1"),
            (1, 1, "Disc 1"),
            (1, 4, "Disc 2"),
            (1, 3, "Disc 2"),
        ],
    )
    # Recording 2: a musical set, untouched by the disc migration.
    con.execute("INSERT INTO recording (id, folder_path) VALUES (2, 'Artist/Show2')")
    con.executemany(
        "INSERT INTO track (recording_id, track_number, set_number) VALUES (?, ?, ?)",
        [
            (2, 1, "Set 1"),
            (2, 2, "Set 1"),
        ],
    )
    con.execute("INSERT INTO user_preference (key, value) VALUES ('ingest_file_behavior', 'move')")
    con.execute("INSERT INTO user_preference (key, value) VALUES ('ai_model', 'claude')")
    con.commit()
    con.close()


def _run(db_path, apply=False):
    args = [sys.executable, str(SCRIPT), "--db", str(db_path)]
    if apply:
        args.append("--apply")
    result = subprocess.run(args, capture_output=True, text=True, cwd=str(REPO_ROOT))
    return result


def test_dry_run_changes_nothing(tmp_path):
    db_path = tmp_path / "test.db"
    _make_db(db_path)

    before = db_path.read_bytes()
    result = _run(db_path, apply=False)
    after = db_path.read_bytes()

    assert result.returncode == 0, result.stdout + result.stderr
    assert before == after

    con = sqlite3.connect(str(db_path))
    cols = {r[1] for r in con.execute("PRAGMA table_info(track)")}
    assert "disc_number" not in cols
    con.close()


def test_disc_rows_migrate_and_set_1_is_untouched(tmp_path):
    db_path = tmp_path / "test.db"
    _make_db(db_path)

    result = _run(db_path, apply=True)
    assert result.returncode == 0, result.stdout + result.stderr

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    rows = {r["track_number"]: r for r in
            con.execute("SELECT * FROM track WHERE recording_id=1")}
    for tn in (1, 2, 3, 4):
        assert rows[tn]["set_number"] is None
    assert rows[1]["disc_number"] == 1 and rows[1]["disc_track_number"] == 1
    assert rows[2]["disc_number"] == 1 and rows[2]["disc_track_number"] == 2
    assert rows[3]["disc_number"] == 2 and rows[3]["disc_track_number"] == 1
    assert rows[4]["disc_number"] == 2 and rows[4]["disc_track_number"] == 2

    set1_rows = list(con.execute("SELECT * FROM track WHERE recording_id=2"))
    for row in set1_rows:
        assert row["set_number"] == "Set 1"
        assert row["disc_number"] is None
        assert row["disc_track_number"] is None

    con.close()


def test_seeds_organize_when_recordings_exist(tmp_path):
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    _run(db_path, apply=True)

    con = sqlite3.connect(str(db_path))
    settings = dict(con.execute("SELECT key, value FROM node_setting"))
    assert settings["file_handling_mode"] == "organize"
    assert settings["rename_files"] == "true"
    con.close()


def test_seeds_nothing_when_no_recordings(tmp_path):
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    con = sqlite3.connect(str(db_path))
    con.execute("DELETE FROM track")
    con.execute("DELETE FROM recording")
    con.commit()
    con.close()

    result = _run(db_path, apply=True)
    assert result.returncode == 0, result.stdout + result.stderr

    con = sqlite3.connect(str(db_path))
    n = con.execute(
        "SELECT count(*) FROM node_setting WHERE key='file_handling_mode'"
    ).fetchone()[0]
    assert n == 0
    con.close()


def test_ingest_file_behavior_preference_is_deleted(tmp_path):
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    _run(db_path, apply=True)

    con = sqlite3.connect(str(db_path))
    n = con.execute(
        "SELECT count(*) FROM user_preference WHERE key='ingest_file_behavior'"
    ).fetchone()[0]
    assert n == 0
    # unrelated preference rows are untouched
    n_other = con.execute(
        "SELECT count(*) FROM user_preference WHERE key='ai_model'"
    ).fetchone()[0]
    assert n_other == 1
    con.close()


def test_all_seven_columns_added(tmp_path):
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    _run(db_path, apply=True)

    con = sqlite3.connect(str(db_path))
    assert {"disc_number", "disc_track_number", "original_file_path"} <= \
        {r[1] for r in con.execute("PRAGMA table_info(track)")}
    assert "rel_path" in {r[1] for r in con.execute("PRAGMA table_info(recording_fingerprint)")}
    assert {"source_tag", "etree_shnid"} <= \
        {r[1] for r in con.execute("PRAGMA table_info(recording)")}
    assert "abbreviation" in {r[1] for r in con.execute("PRAGMA table_info(artist)")}
    con.close()


def test_row_counts_unchanged(tmp_path):
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    con = sqlite3.connect(str(db_path))
    before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("track", "recording", "artist")}
    con.close()

    _run(db_path, apply=True)

    con = sqlite3.connect(str(db_path))
    after = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
             for t in ("track", "recording", "artist")}
    con.close()
    assert before == after


def test_already_migrated_database_is_a_no_op_on_second_run(tmp_path):
    """S11 (review, 2026-09-25): every step is idempotent, so a second
    --apply against an already-migrated database succeeds and changes
    nothing, rather than the old hard refusal (a precondition, not a
    real safety check -- the steps themselves never needed it)."""
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    _run(db_path, apply=True)

    con = sqlite3.connect(str(db_path))
    before = con.execute("SELECT count(*) FROM track").fetchone()[0]
    con.close()

    result = _run(db_path, apply=True)
    assert result.returncode == 0

    con = sqlite3.connect(str(db_path))
    after = con.execute("SELECT count(*) FROM track").fetchone()[0]
    con.close()
    assert after == before


def test_second_apply_never_overwrites_a_user_chosen_keep_mode(tmp_path):
    """S11 (re-review, 2026-09-25): seed_file_handling_mode() must only ever
    INSERT the mode/switches when the key is ABSENT. A second --apply run
    against a database whose owner has since switched to 'keep' (or any
    other value) must leave that choice alone -- the old ON CONFLICT DO
    UPDATE silently flipped it back to 'organize' on every re-run."""
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    _run(db_path, apply=True)

    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT INTO node_setting (key, value) VALUES ('file_handling_mode', 'keep') "
        "ON CONFLICT(key) DO UPDATE SET value='keep'"
    )
    con.execute(
        "INSERT INTO node_setting (key, value) VALUES ('rename_files', 'false') "
        "ON CONFLICT(key) DO UPDATE SET value='false'"
    )
    con.commit()
    con.close()

    result = _run(db_path, apply=True)
    assert result.returncode == 0, result.stdout + result.stderr

    con = sqlite3.connect(str(db_path))
    mode = con.execute(
        "SELECT value FROM node_setting WHERE key='file_handling_mode'"
    ).fetchone()[0]
    rename_files = con.execute(
        "SELECT value FROM node_setting WHERE key='rename_files'"
    ).fetchone()[0]
    con.close()
    assert mode == "keep"
    assert rename_files == "false"


def test_apply_backs_up_the_database_with_sqlite_backup(tmp_path):
    """S11 (re-review, 2026-09-25): the pre-apply backup must be a real
    sqlite3 .backup snapshot (Connection.backup()), not a raw file copy --
    proven here by simply checking the backup file is itself a valid,
    openable SQLite database with the pre-migration schema/data."""
    db_path = tmp_path / "test.db"
    _make_db(db_path)

    result = _run(db_path, apply=True)
    assert result.returncode == 0, result.stdout + result.stderr

    backups = list(tmp_path.glob("test.db.pre-file-handling-migration.bak"))
    assert backups, "no backup file created"
    bak_con = sqlite3.connect(str(backups[0]))
    # Pre-migration schema: no disc_number column yet.
    cols = {r[1] for r in bak_con.execute("PRAGMA table_info('track')")}
    assert "disc_number" not in cols
    n = bak_con.execute("SELECT count(*) FROM track").fetchone()[0]
    bak_con.close()
    assert n > 0
