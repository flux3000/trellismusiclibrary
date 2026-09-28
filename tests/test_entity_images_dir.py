"""
tests/test_entity_images_dir.py — image store moved out of the library
(Bulk Ingest spec chunk 2).

Covers app/utils/entity_images.py::image_dir() and
scripts/migrate_bulk_ingest.py's filesystem move step.
"""

import sqlite3
from io import BytesIO
from pathlib import Path

import pytest

from app.models.venue import Venue


@pytest.fixture()
def api(app):
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


@pytest.fixture()
def venue(app):
    from app.extensions import db as _db
    v = Venue(name="The Fillmore", city="San Francisco", state="CA")
    _db.session.add(v)
    _db.session.commit()
    return v


# ── image_dir() ──────────────────────────────────────────────────────────────

def test_image_dir_under_data_dir_never_library_root(app, tmp_path):
    from app.utils import entity_images as ei

    app.config["LIBRARY_ROOT"] = str(tmp_path / "library")
    app.config["DATA_DIR"] = str(tmp_path / "data")

    with app.app_context():
        d = ei.image_dir("venues", "The Fillmore")

    assert d == Path(tmp_path) / "data" / "images" / "venues" / "The Fillmore"
    assert str(tmp_path / "library") not in str(d)


def test_image_dir_round_trip_artist(api, app, seeded_ids, tmp_path):
    app.config["LIBRARY_ROOT"] = str(tmp_path / "library")
    app.config["DATA_DIR"] = str(tmp_path / "data")
    pid = seeded_ids["artist_id"]

    r = api.post(f"/api/artists/{pid}/images",
                 data={"image": (BytesIO(b"\xff\xd8\xff jpeg"), "photo.jpg")},
                 content_type="multipart/form-data")
    assert r.status_code == 200
    img = r.get_json()["images"][0]

    listed = api.get(f"/api/artists/{pid}/images").get_json()
    assert len(listed) == 1

    assert api.get(f"/api/artists/images/{img['id']}").status_code == 200

    on_disk = tmp_path / "data" / "images" / "artists" / "Bill Evans"
    assert len(list(on_disk.glob("img_*.jpg"))) == 1


def test_image_dir_round_trip_venue(api, app, venue, tmp_path):
    app.config["LIBRARY_ROOT"] = str(tmp_path / "library")
    app.config["DATA_DIR"] = str(tmp_path / "data")

    r = api.post(f"/api/venues/{venue.id}/images",
                 data={"image": (BytesIO(b"\xff\xd8\xff jpeg"), "hall.jpg")},
                 content_type="multipart/form-data")
    assert r.status_code == 200
    img = r.get_json()["images"][0]

    listed = api.get(f"/api/venues/{venue.id}/images").get_json()
    assert len(listed) == 1

    assert api.get(f"/api/venues/images/{img['id']}").status_code == 200

    on_disk = tmp_path / "data" / "images" / "venues" / "The Fillmore"
    assert len(list(on_disk.glob("img_*.jpg"))) == 1


# ── Migration script ─────────────────────────────────────────────────────────

def _make_db(path):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, folder_path TEXT)")
    con.execute("CREATE TABLE artist (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE venue (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE musician (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE event (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO recording (folder_path) VALUES ('x')")
    con.commit()
    con.close()


def _seed_library(lib):
    (lib / "_venues" / "The Fillmore" / "_images").mkdir(parents=True)
    (lib / "_venues" / "The Fillmore" / "_images" / "v.jpg").write_bytes(b"v")
    (lib / "Bill Evans" / "_images").mkdir(parents=True)
    (lib / "Bill Evans" / "_images" / "b.jpg").write_bytes(b"b")


def test_migration_dry_run_moves_nothing(tmp_path, monkeypatch):
    from scripts import migrate_bulk_ingest as mod

    lib = tmp_path / "library"
    data = tmp_path / "data"
    _seed_library(lib)
    db_path = tmp_path / "test.db"
    _make_db(db_path)

    monkeypatch.setattr(mod, "LIBRARY_ROOT", lib)
    monkeypatch.setattr(mod, "_data_dir", lambda: data)
    monkeypatch.setattr(mod, "DEFAULT_DB", db_path)

    import sys
    old_argv = sys.argv
    try:
        sys.argv = ["migrate_bulk_ingest.py", "--db", str(db_path)]
        mod.main()
    finally:
        sys.argv = old_argv

    # Dry run: nothing moved, source files still in place, DATA_DIR untouched.
    assert (lib / "_venues" / "The Fillmore" / "_images" / "v.jpg").exists()
    assert (lib / "Bill Evans" / "_images" / "b.jpg").exists()
    assert not data.exists()


def test_migration_apply_moves_and_is_idempotent(tmp_path, monkeypatch):
    from scripts import migrate_bulk_ingest as mod

    lib = tmp_path / "library"
    data = tmp_path / "data"
    _seed_library(lib)
    db_path = tmp_path / "test.db"
    _make_db(db_path)

    monkeypatch.setattr(mod, "LIBRARY_ROOT", lib)
    monkeypatch.setattr(mod, "_data_dir", lambda: data)

    import sys
    old_argv = sys.argv
    try:
        sys.argv = ["migrate_bulk_ingest.py", "--db", str(db_path), "--apply"]
        mod.main()
    finally:
        sys.argv = old_argv

    assert (data / "images" / "venues" / "The Fillmore" / "v.jpg").exists()
    assert (data / "images" / "artists" / "Bill Evans" / "b.jpg").exists()
    assert not (lib / "_venues" / "The Fillmore" / "_images").exists()
    assert not (lib / "Bill Evans" / "_images").exists()

    con = sqlite3.connect(str(db_path))
    cols = {r[1] for r in con.execute("PRAGMA table_info(recording)")}
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    n_recordings = con.execute("SELECT count(*) FROM recording").fetchone()[0]
    con.close()
    assert "kind" in cols
    assert {"bulk_ingest_run", "bulk_ingest_item"} <= tables
    assert n_recordings == 1

    # Second run: no-op, nothing left to move, no error.
    try:
        sys.argv = ["migrate_bulk_ingest.py", "--db", str(db_path), "--apply"]
        mod.main()
    finally:
        sys.argv = old_argv


def test_migration_image_move_survives_cross_device_rename(tmp_path, monkeypatch):
    """S1: Path.rename raises EXDEV when LIBRARY_ROOT and DATA_DIR are on
    different filesystems (SMB library, local DATA_DIR on the real install).
    shutil.move must be used instead of Path.rename so the move still
    completes."""
    import errno
    import os as _os
    from scripts import migrate_bulk_ingest as mod

    lib = tmp_path / "library"
    data = tmp_path / "data"
    _seed_library(lib)
    db_path = tmp_path / "test.db"
    _make_db(db_path)

    monkeypatch.setattr(mod, "LIBRARY_ROOT", lib)
    monkeypatch.setattr(mod, "_data_dir", lambda: data)

    real_rename = _os.rename
    def fake_rename(src, dst, *a, **kw):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(_os, "rename", fake_rename)

    import sys
    old_argv = sys.argv
    try:
        sys.argv = ["migrate_bulk_ingest.py", "--db", str(db_path), "--apply"]
        mod.main()
    finally:
        sys.argv = old_argv
        monkeypatch.setattr(_os, "rename", real_rename)

    assert (data / "images" / "venues" / "The Fillmore" / "v.jpg").exists()
    assert (data / "images" / "artists" / "Bill Evans" / "b.jpg").exists()
    assert not (lib / "_venues" / "The Fillmore" / "_images").exists()
    assert not (lib / "Bill Evans" / "_images").exists()


def test_migration_adds_recording_folder_path_index(tmp_path, monkeypatch):
    """N4: Recording.folder_path is looked up by exact value once per
    bulk_ingest item; without an index that is a full table scan that grows
    with the library."""
    from scripts import migrate_bulk_ingest as mod

    lib = tmp_path / "library"
    data = tmp_path / "data"
    _seed_library(lib)
    db_path = tmp_path / "test.db"
    _make_db(db_path)

    monkeypatch.setattr(mod, "LIBRARY_ROOT", lib)
    monkeypatch.setattr(mod, "_data_dir", lambda: data)

    import sys
    old_argv = sys.argv
    try:
        sys.argv = ["migrate_bulk_ingest.py", "--db", str(db_path), "--apply"]
        mod.main()
    finally:
        sys.argv = old_argv

    con = sqlite3.connect(str(db_path))
    indexes = {r[1] for r in con.execute("PRAGMA index_list('recording')")}
    con.close()
    assert "ix_recording_folder_path" in indexes
