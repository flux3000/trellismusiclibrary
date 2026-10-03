"""
tests/test_bulk_ingest_any_source.py -- Bulk Ingest takes any source folder
(in-place cataloging vs bring-in) and runs a small-runs-first queue.

Same style as test_bulk_ingest_run.py: real FLACs under tmp dirs, discover()/
process() driven directly, no worker thread except where noted.
"""

import hashlib
import os

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from app.extensions import db as _db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.recording import Recording
from app.models.recording_event import RecordingEvent
from app.models.user import User
from app.utils import bulk_ingest_run as bir
from app.utils import download_queue as dq
from app.utils import node_settings


def _flac(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    tags.setdefault("TRACKNUMBER", "1")
    for k, v in tags.items():
        audio[k] = v
    audio.save()


def _show(root, name, day=8, **extra):
    _flac(root / name / "01.flac", ARTIST="Grateful Dead",
          DATE=f"1977-05-{day:02d}", VENUE=f"Hall {name}", **extra)


def _hashes(root):
    out = {}
    for dp, _d, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dp, f)
            out[os.path.relpath(p, root)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def _never_stop():
    return False


@pytest.fixture()
def env(app, tmp_path, monkeypatch, seeded_ids):
    """allowed/ holds the library and an incoming folder; elsewhere/ is outside
    IMPORT_ROOTS. Follow-up enqueueing is stubbed (R2-N4)."""
    allowed = tmp_path / "allowed"
    lib = allowed / "lib"
    incoming = allowed / "incoming"
    lib.mkdir(parents=True)
    incoming.mkdir()
    (tmp_path / "elsewhere").mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["IMPORT_ROOTS"] = [str(allowed)]
    app.config["IMPORT_DIR"] = str(incoming)
    app.config["LOGIN_DISABLED"] = True
    monkeypatch.setattr("app.api.ingest._enqueue", lambda app, kind, item_id, run_id=None: True)
    monkeypatch.setattr(bir, "_start_worker", lambda *a, **k: None)
    return type("Env", (), dict(allowed=allowed, lib=lib, incoming=incoming,
                                elsewhere=tmp_path / "elsewhere"))


def _run(root, status="running"):
    run = BulkIngestRun(root=str(root), status=status)
    _db.session.add(run)
    _db.session.commit()
    return run


def _item(run, rel="Show"):
    return (_db.session.query(BulkIngestItem)
            .filter_by(run_id=run.id, rel_path=rel).first())


# ── placement ───────────────────────────────────────────────────────────────

def test_in_place_source_stays_put_and_is_not_tagged(env):
    node_settings.apply_mode("organize")
    assert node_settings.get_file_handling()["write_tags_on_ingest"] is True
    _show(env.lib, "Show", TITLE="Wrong Title")
    before = _hashes(env.lib)

    run = _run(env.lib)
    bir.discover(run)
    bir.process(run, _never_stop)

    assert _hashes(env.lib) == before
    item = _item(run)
    assert item.status == "ingested"
    rec = _db.session.get(Recording, item.recording_id)
    assert rec.folder_path == "Show"
    assert _db.session.query(RecordingEvent).filter_by(
        recording_id=rec.id, event_type="tags_written").count() == 0


def test_outside_source_is_brought_in_and_tagged(env):
    node_settings.apply_mode("organize")
    assert node_settings.get_file_handling()["write_tags_on_ingest"] is True
    _show(env.incoming, "Show", TITLE="Wrong Title")

    run = _run(env.incoming)
    bir.discover(run)
    bir.process(run, _never_stop)

    item = _item(run)
    assert item.status == "ingested", (item.status, item.reason, item.detail)
    assert not (env.incoming / "Show").exists()          # moved, not copied
    rec = _db.session.get(Recording, item.recording_id)
    assert rec.folder_path and rec.folder_path != "Show"
    assert os.path.isdir(env.lib / rec.folder_path)
    assert _db.session.query(RecordingEvent).filter_by(
        recording_id=rec.id, event_type="tags_written").count() == 1


# ── start endpoint validation ───────────────────────────────────────────────

def _admin(client):
    user = _db.session.query(User).filter_by(username="admin").first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_start_rejects_bad_paths(app, env):
    c = app.test_client()
    _admin(c)
    (env.allowed / "afile").write_text("x")
    bad = [
        str(env.allowed / "missing"),            # does not exist
        str(env.allowed / "afile"),              # not a directory
        str(env.allowed),                        # ancestor of the library
    ]
    for path in bad:
        r = c.post("/api/bulk-ingest/start", json={"path": path})
        assert r.status_code == 400, path
        assert "error" in r.get_json()
    assert _db.session.query(BulkIngestRun).count() == 0


def test_start_accepts_any_folder_for_the_admin(app, env):
    # Ryan, 2026-10-02: the admin's music can live anywhere, so IMPORT_ROOTS
    # does not bind them; a folder outside the roots starts a run.
    c = app.test_client()
    _admin(c)
    r = c.post("/api/bulk-ingest/start", json={"path": str(env.elsewhere)})
    assert r.status_code == 200, r.get_json()


def test_import_roots_still_bind_outside_an_admin_request(app, env):
    from app.utils.paths import within_import_roots
    with app.app_context():
        assert within_import_roots(str(env.elsewhere)) is False


def test_start_placement_and_idempotence(app, env):
    c = app.test_client()
    _admin(c)
    (env.lib / "Artist").mkdir()

    default = c.post("/api/bulk-ingest/start").get_json()
    assert default["root"] == str(env.lib) and default["placement"] == "in_place"
    sub = c.post("/api/bulk-ingest/start", json={"path": str(env.lib / "Artist")}).get_json()
    assert sub["placement"] == "in_place" and sub["id"] != default["id"]
    out = c.post("/api/bulk-ingest/start", json={"path": str(env.incoming)}).get_json()
    assert out["placement"] == "bring_in"
    again = c.post("/api/bulk-ingest/start", json={"path": str(env.incoming)}).get_json()
    assert again["id"] == out["id"]

    runs = c.get("/api/bulk-ingest/runs").get_json()["runs"]
    assert [r["id"] for r in runs] == [default["id"], sub["id"], out["id"]]
    assert c.get(f"/api/bulk-ingest/current?run_id={out['id']}").get_json()["id"] == out["id"]


# ── rel_path and duplicate keys ─────────────────────────────────────────────

def test_library_subfolder_rel_path_equals_folder_path(env):
    _show(env.lib / "Grateful Dead", "Show")
    run = _run(env.lib / "Grateful Dead")
    bir.discover(run)
    bir.process(run, _never_stop)

    item = _item(run, "Grateful Dead/Show")
    assert item is not None and item.status == "ingested"
    assert _db.session.get(Recording, item.recording_id).folder_path == "Grateful Dead/Show"


def test_same_rel_path_under_another_root_is_not_ingested(env):
    other = env.allowed / "other"
    _show(env.incoming, "Show")
    _show(env.lib, "Show", day=9)
    # "Show" was ingested from `other` and "Show" also sits in the library.
    done = _run(other, status="done")
    _db.session.add(BulkIngestItem(run_id=done.id, rel_path="Show", status="ingested"))
    done_lib = _run(env.lib, status="done")
    _db.session.add(BulkIngestItem(run_id=done_lib.id, rel_path="Show", status="ingested"))
    _db.session.commit()

    run = _run(env.incoming)
    bir.discover(run)
    assert _item(run).status == "pending"

    # ...while the same folder under the library root IS recognised.
    run2 = _run(env.lib)
    bir.discover(run2)
    assert _item(run2) is None


def test_outside_item_ignores_recording_with_same_folder_path(env, seeded_ids):
    _db.session.add(Recording(performance_id=seeded_ids["performance_id"],
                              folder_path="Show", is_complete=True, is_official=False))
    _db.session.commit()
    _show(env.incoming, "Show")
    run = _run(env.incoming)
    bir.discover(run)
    assert _item(run).status == "pending"       # not skipped/already_in_library


# ── folders still downloading ───────────────────────────────────────────────

def test_downloading_folder_is_skipped_then_ingested(env, monkeypatch):
    busy = {"Busy"}
    monkeypatch.setattr(dq, "is_busy_folder",
                        lambda path: os.path.basename(path.rstrip(os.sep)) in busy)
    _show(env.incoming, "Busy")
    _show(env.incoming, "Idle", day=9)

    run = _run(env.incoming)
    bir.discover(run)
    bir.process(run, _never_stop)

    assert _item(run, "Idle").status == "ingested"
    b = _item(run, "Busy")
    assert b.status == "pending" and b.reason == "downloading"
    assert (env.incoming / "Busy").is_dir()
    assert _db.session.get(BulkIngestRun, run.id).status == "running"   # not done

    busy.clear()
    monkeypatch.setattr(bir, "_DOWNLOAD_RETRY_SECS", 0)
    bir.process(run, _never_stop)
    b = _item(run, "Busy")
    assert b.status == "ingested" and b.reason is None
    assert _db.session.get(BulkIngestRun, run.id).status == "done"


# ── queue: small runs first ─────────────────────────────────────────────────

def _drive(order, steps=50):
    """The worker's loop, one item per pick, recording which run each pick served."""
    discovered = set()
    for _ in range(steps):
        r = bir._pick_run(discovered)
        if r is None:
            break
        order.append(r.id)
        bir.process(r, bir._one_item_flag(r.id))


def test_small_run_jumps_ahead_of_large_run(env):
    for i in range(1, 5):
        _show(env.lib, f"Lib{i}", day=i)
    _show(env.incoming, "Small", day=20)

    big = _run(env.lib)
    discovered = set()
    first = bir._pick_run(discovered)
    assert first.id == big.id
    bir.process(first, bir._one_item_flag(big.id))     # big run is mid-flight

    small = _run(env.incoming)                          # started during it
    order = []
    _drive(order)

    # small run served right after the in-flight item, big run resumes after.
    assert order[0] == small.id
    assert order.count(small.id) == 2    # its item, then the pass that closes it
    assert set(order[1:]) <= {big.id, small.id}
    for run in (big, small):
        assert _db.session.get(BulkIngestRun, run.id).status == "done"
    assert _item(small, "Small").status == "ingested"


def test_paused_run_does_not_block_others(env):
    for i in range(1, 4):
        _show(env.lib, f"Lib{i}", day=i)
    _show(env.incoming, "Small", day=20)
    big = _run(env.lib, status="paused")
    small = _run(env.incoming)
    bir.discover(big)

    order = []
    _drive(order)
    assert set(order) == {small.id}
    assert _item(small, "Small").status == "ingested"
    assert _db.session.query(BulkIngestItem).filter_by(
        run_id=big.id, status="pending").count() == 3

    bir.resume_run(_db.session.get(BulkIngestRun, big.id))
    order = []
    _drive(order)
    assert set(order) == {big.id}
    assert _db.session.get(BulkIngestRun, big.id).status == "done"


def test_resume_on_boot_resumes_every_unfinished_run(app, env, monkeypatch):
    a, b = _run(env.lib), _run(env.incoming)
    p = _run(env.allowed, status="paused")
    started = []
    monkeypatch.setattr(bir, "_start_worker", lambda *a, **k: started.append(1))
    monkeypatch.setattr("app.utils.bulk_ingest_followup.enqueue_followups", lambda *a, **k: None)
    (_db.session.query(BulkIngestItem).filter_by(run_id=a.id).delete())
    _db.session.add(BulkIngestItem(run_id=a.id, rel_path="X", status="in_progress"))
    _db.session.add(BulkIngestItem(run_id=b.id, rel_path="Y", status="in_progress"))
    _db.session.commit()
    bir.resume_on_boot(app)
    _db.session.expire_all()
    assert _db.session.query(BulkIngestItem).filter_by(status="in_progress").count() == 0
    assert started == [1]
    assert _db.session.get(BulkIngestRun, p.id).status == "paused"


# ── a run pointed at ONE show folder ────────────────────────────────────────

def test_outside_single_show_folder_run_ingests_one_recording(env):
    _show(env.incoming, "Show")
    run = _run(env.incoming / "Show")
    bir.discover(run)
    items = _db.session.query(BulkIngestItem).filter_by(run_id=run.id).all()
    assert len(items) == 1
    bir.process(run, _never_stop)
    item = _db.session.get(BulkIngestItem, items[0].id)
    assert item.status == "ingested", (item.status, item.reason, item.detail)
    assert not (env.incoming / "Show").exists()
    assert os.path.isdir(env.lib / _db.session.get(Recording, item.recording_id).folder_path)
    assert _db.session.get(BulkIngestRun, run.id).status == "done"


def test_multi_disc_single_show_folder_is_one_item(env):
    show = env.incoming / "Multi"
    for d in ("Disc 1", "Disc 2"):
        _flac(show / d / "01.flac", ARTIST="Grateful Dead", DATE="1977-05-08", VENUE="Hall")
    run = _run(show)
    bir.discover(run)
    assert _db.session.query(BulkIngestItem).filter_by(run_id=run.id).count() == 1


def test_in_library_single_show_folder_is_cataloged_in_place(env):
    _show(env.lib / "Grateful Dead", "Show")
    before = _hashes(env.lib)
    run = _run(env.lib / "Grateful Dead" / "Show")
    bir.discover(run)
    item = _item(run, "Grateful Dead/Show")
    assert item is not None
    assert _db.session.query(BulkIngestItem).filter_by(run_id=run.id).count() == 1
    bir.process(run, _never_stop)
    item = _item(run, "Grateful Dead/Show")
    assert item.status == "ingested"
    assert _db.session.get(Recording, item.recording_id).folder_path == "Grateful Dead/Show"
    assert _hashes(env.lib) == before


def test_runs_waiting_counts_a_folder_once_across_runs(app, env):
    # 2026-10-02: a one-folder Downloads run and a later run over all of
    # Downloads both held the same show, and the badge read 4 for 2 rows.
    show = env.incoming / "ShowA"
    show.mkdir()
    one = _run(show, status="paused")
    _db.session.add(BulkIngestItem(run_id=one.id, rel_path=".", status="review"))
    whole = _run(env.incoming, status="paused")
    _db.session.add(BulkIngestItem(run_id=whole.id, rel_path="ShowA", status="review"))
    _db.session.add(BulkIngestItem(run_id=whole.id, rel_path="ShowB", status="ready"))
    _db.session.commit()
    c = app.test_client()
    _admin(c)
    assert c.get("/api/bulk-ingest/runs").get_json()["waiting"] == 2
