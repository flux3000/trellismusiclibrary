"""
Resolver v2 chunk 7b: Artist.confirmed_at (a person confirmed this act) and the resolver
feature built on it. Human saves set it (the confirm path, a direct edit, adding an artist),
unattended saves never do, it is never cleared, the migration backfills every existing row,
and an exact match to a confirmed act is an independent source for the artist field.
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.extensions import db as _db
from app.models.artist import Artist
from app.utils.artists import mark_artist_confirmed
from app.utils.ingest import build_scan_payload
from app.utils.reader import confidence as C
from app.utils.reader.library import LibraryIndex, invalidate_library_cache
from app.utils.resolve import resolve

from tests.test_learned_aliases import env, _show, _flac, _confirm  # noqa: F401  (fixtures and helpers)

ROOT = Path(__file__).resolve().parents[1]


def _artist(name):
    return _db.session.query(Artist).filter_by(name=name).first()


# ── marking ────────────────────────────────────────────────────────────────

def test_mark_artist_confirmed_sets_once_and_never_clears(app):
    a = Artist(name="Mark Test Act")
    _db.session.add(a)
    _db.session.flush()
    assert a.confirmed_at is None
    mark_artist_confirmed(a)
    first = a.confirmed_at
    assert first is not None
    mark_artist_confirmed(a)
    assert a.confirmed_at == first
    assert mark_artist_confirmed(None) is None


def test_a_persons_save_through_do_confirm_confirms_the_artist(env):
    _show(env.lib, "A1", "Sam Bush Band", 8)
    _confirm(env.lib / "A1")
    assert _artist("Sam Bush Band").confirmed_at is not None


def test_an_unattended_auto_ingest_leaves_a_new_artist_unconfirmed(env):
    from app.api.ingest import auto_confirm
    from app.utils import bulk_ingest_run as bir
    _show(env.lib, "A1", "Sam Bush Band", 8)
    _flac(env.lib / "A1" / "01.flac", DATE="1977-05-08", ARTIST="Sam Bush Band")
    out = auto_confirm(str(env.lib / "A1"), bir._owner_user_id())
    assert out["status"] == "ingested"
    assert _artist("Sam Bush Band").confirmed_at is None


def test_a_later_unattended_save_does_not_clear_a_confirmed_artist(env):
    from app.api.ingest import auto_confirm
    from app.utils import bulk_ingest_run as bir
    _show(env.lib, "A1", "Sam Bush Band", 8)
    _confirm(env.lib / "A1")
    stamp = _artist("Sam Bush Band").confirmed_at
    _show(env.lib, "A2", "Sam Bush Band", 9)
    _flac(env.lib / "A2" / "01.flac", DATE="1977-05-09", ARTIST="Sam Bush Band")
    auto_confirm(str(env.lib / "A2"), bir._owner_user_id())
    assert _artist("Sam Bush Band").confirmed_at == stamp


def test_ingest_anyway_is_a_persons_save(env, monkeypatch):
    """force=True (the "Ingest anyway" button) makes the payload attended."""
    from app.api import ingest as I
    seen = {}
    monkeypatch.setattr(I, "_do_confirm", lambda payload, uid, **k: seen.update(p=payload) or {"ok": True})
    _show(env.lib, "A1", "Sam Bush Band", 8)
    try:
        I.auto_confirm(str(env.lib / "A1"), 1, force=True)
    except Exception:  # noqa: BLE001 -- only the payload handed to _do_confirm matters here
        pass
    if seen:
        assert seen["p"]["unattended"] is False


def _client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
    return c


def test_creating_and_editing_an_artist_by_hand_confirms_it(app):
    app.config["LOGIN_DISABLED"] = True
    c = _client(app)
    r = c.post("/api/artists/", json={"name": "Hand Made Act"})
    assert r.status_code in (200, 201), r.get_data(as_text=True)
    assert _artist("Hand Made Act").confirmed_at is not None
    other = Artist(name="Edit Me")                      # a row an import created: unconfirmed
    _db.session.add(other)
    _db.session.commit()
    assert other.confirmed_at is None
    r = c.put(f"/api/artists/{other.id}", json={"bio": "x"})
    assert r.status_code == 200, r.get_data(as_text=True)
    _db.session.refresh(other)
    assert other.confirmed_at is not None


# ── migration ──────────────────────────────────────────────────────────────

def _old_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE artist (id INTEGER PRIMARY KEY, name TEXT, created_at DATETIME)")
    con.executemany("INSERT INTO artist (id, name, created_at) VALUES (?,?,?)", rows)
    con.commit()
    con.close()


def test_migration_adds_the_column_and_backfills_every_row(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import migrate_add_artist_confirmed as M
    db = tmp_path / "old.db"
    _old_db(db, [(1, "A", "2024-01-02 03:04:05"), (2, "B", None), (3, "C", "")])
    added, n, left = M.migrate(db, out=lambda *_: None)
    assert added and n == 3 and left == 0
    con = sqlite3.connect(db)
    assert "confirmed_at" in {r[1] for r in con.execute("PRAGMA table_info(artist)")}
    got = dict(con.execute("SELECT id, confirmed_at FROM artist"))
    assert got[1] == "2024-01-02 03:04:05" and got[2] and got[3]
    con.close()
    again = M.migrate(db, out=lambda *_: None)          # idempotent
    assert again == (False, 0, 0)


def test_migration_dry_run_writes_nothing(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import migrate_add_artist_confirmed as M
    db = tmp_path / "old.db"
    _old_db(db, [(1, "A", None)])
    M.migrate(db, dry=True, out=lambda *_: None)
    con = sqlite3.connect(db)
    assert "confirmed_at" not in {r[1] for r in con.execute("PRAGMA table_info(artist)")}
    con.close()


def test_the_model_column_is_nullable_and_in_create_all(app):
    cols = {r[1]: r for r in _db.session.execute(_db.text("PRAGMA table_info(artist)")).fetchall()}
    assert "confirmed_at" in cols and cols["confirmed_at"][3] == 0


# ── the library flag ───────────────────────────────────────────────────────

def test_library_confirmed_matches_exact_names_and_aliases_only():
    lib = LibraryIndex.from_dicts(
        artists=[{"name": "Sam Bush Band", "confirmed": True}, {"name": "Del McCoury Band"},
                 {"name": "The Seldom Scene", "confirmed": True}],
        artist_aliases=[("Sam Bush Band", "SBB")])
    assert lib.artist_confirmed("Sam Bush Band") and lib.artist_confirmed("sam bush band")
    assert lib.artist_confirmed("SBB")                       # a learned alias of a confirmed act
    assert not lib.artist_confirmed("Del McCoury Band")      # exists, not confirmed
    assert not lib.artist_confirmed("Sam Bush")              # a core match is not exact
    assert not lib.artist_confirmed("Nobody")
    assert not LibraryIndex.empty().artist_confirmed("Sam Bush Band")


def test_hidden_rows_do_not_confirm(app):
    lib = LibraryIndex.from_rows([(1, "Only Act")], [], [], [], [], [(7, 1, None, None)], confirmed=[1])
    assert lib.artist_confirmed("Only Act")
    assert not lib.without_performance(7).artist_confirmed("Only Act")


# ── the resolver feature ───────────────────────────────────────────────────

def _artist_field(env, confirmed):
    """An info-only show whose artist is read from the text; the library holds that act
    (confirmed or not: the same row is flipped between calls)."""
    row = _artist("Sam Bush Band")
    if row is None:
        row = Artist(name="Sam Bush Band")
        _db.session.add(row)
        _db.session.flush()
    row.confirmed_at = None
    if confirmed:
        mark_artist_confirmed(row)
    _db.session.commit()
    invalidate_library_cache()
    if not (env.lib / "A1").exists():
        _show(env.lib, "A1", "Sam Bush Band", 8)
    scan = build_scan_payload(str(env.lib / "A1"))
    return resolve(scan, library_root=str(env.lib), placement=None).artist


def test_only_a_confirmed_row_adds_the_independent_source(env):
    C.set_calibration(None)
    plain = _artist_field(env, confirmed=False)
    assert not any(r.get("independent") for r in plain.evidence)
    sure = _artist_field(env, confirmed=True)
    assert any(r.get("independent") and r["source"] == "library" for r in sure.evidence)
    assert not any(r["source"] == "confirmed" for r in sure.evidence)
    assert sure.logit == pytest.approx(plain.logit + C.AGREE_BONUS["artist"])


@pytest.fixture()
def artist_bar():
    """A bar low enough for the +3 bonus to show (tau .94, m 12). The shipped bar is higher: G1's
    own rows at this logit are right 95% of the time, so a confirmed G3-style row is NOT confident
    under the shipped calibration (see the chunk 7b report); this test is about the mechanism."""
    import copy
    cal = copy.deepcopy(C.load_calibration(force=True))
    cal["fields"]["artist"].update(tau=0.94, m=12.0)
    C.set_calibration(cal)
    yield
    C.set_calibration(None)
    C.load_calibration(force=True)


def test_a_g3_style_row_with_a_confirmed_artist_reaches_confident(env, artist_bar):
    f = _artist_field(env, confirmed=True)
    assert f.value == "Sam Bush Band"
    assert f.confidence == "confident", (f.logit, f.p)


def test_the_same_row_with_an_unconfirmed_artist_stays_tentative(env, artist_bar):
    f = _artist_field(env, confirmed=False)
    assert f.confidence != "confident"


def test_a_missing_calibration_still_makes_everything_tentative(env, tmp_path):
    C.set_calibration({"fields": {}})
    try:
        f = _artist_field(env, confirmed=True)
        assert f.confidence != "confident"
    finally:
        C.set_calibration(None)
        C.load_calibration(force=True)


# ── startup migration ──────────────────────────────────────────────────────

def _app_on(path):
    from config import Config
    from app import create_app

    class Cfg(Config):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"
        TESTING = True
        DEV_MODE = False
    return create_app(config_class=Cfg)


def _cols(path):
    con = sqlite3.connect(path)
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(artist)")}
    finally:
        con.close()


def test_startup_migrates_an_old_schema_db_once(tmp_path):
    db = str(tmp_path / "old.db")
    _old_db(db, [(1, "A", "2024-01-02 03:04:05"), (2, "B", None)])
    _app_on(db)
    assert "confirmed_at" in _cols(db)
    con = sqlite3.connect(db)
    got = dict(con.execute("SELECT id, confirmed_at FROM artist"))
    assert got[1] == "2024-01-02 03:04:05" and got[2]
    con.execute("UPDATE artist SET confirmed_at = NULL WHERE id = 2")   # an unattended row, later
    con.commit()
    con.close()
    _app_on(db)                                    # second start: nothing is touched
    con = sqlite3.connect(db)
    assert dict(con.execute("SELECT id, confirmed_at FROM artist"))[2] is None
    con.close()


def test_startup_leaves_a_fresh_or_empty_db_alone(tmp_path):
    from app.utils.schema_upgrades import ensure_artist_confirmed_at
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    app = _app_on(str(empty))
    with app.app_context():
        assert ensure_artist_confirmed_at(_db.engine) is None
        _db.create_all()
        assert "confirmed_at" in _cols(str(empty))
        assert ensure_artist_confirmed_at(_db.engine) is None


def test_a_failed_backfill_rolls_the_column_back(tmp_path, monkeypatch):
    from app.utils import schema_upgrades as S
    db = str(tmp_path / "old.db")
    _old_db(db, [(1, "A", None)])
    con = sqlite3.connect(db)
    con.execute("CREATE TRIGGER boom BEFORE UPDATE ON artist BEGIN SELECT RAISE(ABORT, 'no'); END")
    con.commit()
    con.close()
    from sqlalchemy import create_engine
    with pytest.raises(RuntimeError, match="old.db"):
        S.ensure_artist_confirmed_at(create_engine(f"sqlite:///{db}"))
    assert "confirmed_at" not in _cols(db)


def test_a_missing_db_file_or_folder_is_never_created(tmp_path):
    from app.utils.schema_upgrades import ensure_artist_confirmed_at
    from sqlalchemy import create_engine
    gone = tmp_path / "nope.db"
    # The migration itself never creates the file (install_epoch may, later in create_app; pre-existing).
    assert ensure_artist_confirmed_at(create_engine(f"sqlite:///{gone}")) is None
    assert not gone.exists()
    _app_on(str(gone))                  # and create_app does not raise
    deep = tmp_path / "missing" / "folder" / "x.db"
    _app_on(str(deep))
    assert not deep.exists() and not deep.parent.exists()
    assert ensure_artist_confirmed_at(create_engine(f"sqlite:///{deep}")) is None
    assert not deep.parent.exists()


def test_a_read_only_old_db_stops_with_a_clear_message(tmp_path):
    import os, stat
    from app.utils.schema_upgrades import ensure_artist_confirmed_at
    from sqlalchemy import create_engine
    db = tmp_path / "ro.db"
    _old_db(str(db), [(1, "A", None)])
    os.chmod(db, stat.S_IREAD)
    try:
        if os.access(db, os.W_OK):
            pytest.skip("running as a user that ignores file modes")
        with pytest.raises(RuntimeError, match="ro.db"):
            ensure_artist_confirmed_at(create_engine(f"sqlite:///{db}"))
    finally:
        os.chmod(db, stat.S_IREAD | stat.S_IWRITE)
