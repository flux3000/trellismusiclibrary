"""
tests/test_bulk_review_v2.py -- fixes from the second independent review of
Bulk Ingest (Context Library/Bulk Ingest -- Independent Review v2.md).

R2-1 (nested unreadable folders), R2-2 (NFD on-disk folder names), R2-3
(resume/worker-exit race), R2-4 (root-level checksum files with per-disc
subpaths), R2-5 (natural sort of unpadded filenames), and the nits that
change behavior (N5 worker exceptions pause the run, N6 a rejected review
row reconciles to skipped).
"""

import os
import threading
import time
import unicodedata
from unittest import mock

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from app.extensions import db as _db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.recording import Recording
from app.models.quality import QualityAnalysis
from app.utils import bulk_ingest_run


def _flac(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    tags.setdefault("TRACKNUMBER", "1")
    for k, v in tags.items():
        audio[k] = v
    audio.save()


def _never_stop():
    return False


@pytest.fixture(autouse=True)
def _no_real_followup_thread(monkeypatch):
    """R2-N4: every test in this file drives a run to 'done', which queues
    the real follow-up pass (score / mb_artist) against this test's own temp
    DB via a background thread. Left unpatched, that thread can still be
    running when the test's fixture drops the DB out from under it, which
    prints a swallowed OperationalError on some other test's watch."""
    monkeypatch.setattr("app.api.ingest._enqueue", lambda app, kind, item_id: True)


# ── R2-1: nested unreadable folders must not vanish ─────────────────────────

@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_nested_unreadable_show_is_failed_not_vanished(app, tmp_path):
    root = tmp_path / "Library"
    root.mkdir()
    _flac(root / "Artist" / "a1977-05-08" / "01.flac",
          ARTIST="A", DATE="1977-05-08", VENUE="V")
    _flac(root / "Artist" / "a1977-05-09" / "01.flac",
          ARTIST="A", DATE="1977-05-09", VENUE="V")
    _flac(root / "Solo" / "s1977-05-10" / "01.flac",
          ARTIST="A", DATE="1977-05-10", VENUE="V")
    os.chmod(root / "Artist" / "a1977-05-09", 0)
    os.chmod(root / "Solo" / "s1977-05-10", 0)
    try:
        app.config["LIBRARY_ROOT"] = str(root)
        run = BulkIngestRun(root=str(root), status="running")
        _db.session.add(run)
        _db.session.commit()
        bulk_ingest_run.discover(run)
        bulk_ingest_run.process(run, _never_stop)

        items = {it.rel_path: it for it in
                 _db.session.query(BulkIngestItem).filter_by(run_id=run.id).all()}
        assert items["Artist/a1977-05-08"].status == "ingested"
        assert items["Artist/a1977-05-09"].status == "failed"
        assert items["Artist/a1977-05-09"].reason == "unreadable"
        assert items["Artist/a1977-05-09"].detail
        assert items["Solo/s1977-05-10"].status == "failed"
        assert items["Solo/s1977-05-10"].reason == "unreadable"
    finally:
        os.chmod(root / "Artist" / "a1977-05-09", 0o755)
        os.chmod(root / "Solo" / "s1977-05-10", 0o755)


# ── R2-2: an NFD on-disk folder name must still be found and ingested ────────

def test_nfd_on_disk_folder_name_ingests_with_nfc_folder_path(app, tmp_path):
    root = tmp_path / "Library"
    root.mkdir()
    nfd_artist = unicodedata.normalize("NFD", "Lucía")
    _flac(root / nfd_artist / "lu2001-01-01" / "01.flac",
          ARTIST="Lucía", DATE="2001-01-01", VENUE="V")

    app.config["LIBRARY_ROOT"] = str(root)
    run = BulkIngestRun(root=str(root), status="running")
    _db.session.add(run)
    _db.session.commit()
    bulk_ingest_run.discover(run)
    bulk_ingest_run.process(run, _never_stop)

    item = (_db.session.query(BulkIngestItem).filter_by(run_id=run.id).first())
    assert item.status == "ingested", (item.status, item.reason, item.detail)
    rec = _db.session.get(Recording, item.recording_id)
    assert rec.folder_path == unicodedata.normalize("NFC", rec.folder_path)
    # The staging/dedup key (rel_path) was always NFC -- unchanged by this fix.
    assert item.rel_path == unicodedata.normalize("NFC", item.rel_path)


# ── R2-3: a Resume landing in the worker's exit window must not get stuck ──

def test_resume_during_worker_exit_does_not_strand_the_run(app, tmp_path):
    root = tmp_path / "Library"
    root.mkdir()
    for i in range(1, 5):
        _flac(root / f"Show{i}" / "01.flac", ARTIST="A", DATE=f"1977-05-0{i}", VENUE="V")
    app.config["LIBRARY_ROOT"] = str(root)

    state = {"worker": None}
    orig_process = bulk_ingest_run.process

    def wrapped_process(run, stop_flag):
        state["worker"] = threading.current_thread()
        return orig_process(run, stop_flag)

    from sqlalchemy.orm import scoped_session
    orig_remove = scoped_session.remove

    def slow_remove(self):
        # Simulates the app-context teardown taking a moment right as the
        # worker has decided to leave -- the exact window R2-3 closes.
        if threading.current_thread() is state["worker"]:
            time.sleep(1.0)
        return orig_remove(self)

    from app.api import ingest as ing
    orig_confirm = ing._do_confirm

    def slow_confirm(data, uid, **kw):
        time.sleep(0.3)
        return orig_confirm(data, uid, **kw)

    with mock.patch.object(bulk_ingest_run, "process", wrapped_process), \
         mock.patch.object(scoped_session, "remove", slow_remove), \
         mock.patch.object(ing, "_do_confirm", slow_confirm):
        run = bulk_ingest_run.start_run(str(root))
        time.sleep(0.5)
        bulk_ingest_run.pause_run(run)
        # Give the worker time to read 'paused' and enter its exit path
        # (inside slow_remove's sleep) before Resume arrives.
        time.sleep(0.3)
        bulk_ingest_run.resume_run(run)
        time.sleep(3.0)

        _db.session.expire_all()
        r = _db.session.get(BulkIngestRun, run.id)
        deadline = time.time() + 5
        while r.status == "running" and time.time() < deadline:
            time.sleep(0.2)
            _db.session.expire_all()
            r = _db.session.get(BulkIngestRun, run.id)

        assert r.status == "done", "run must finish, not get stuck 'running' with no worker"
        items = _db.session.query(BulkIngestItem).filter_by(run_id=run.id).all()
        assert all(it.status == "ingested" for it in items)


# ── R2-4: a root-level checksum file listing per-disc subpaths ─────────────

def test_rootlevel_checksum_file_matches_each_disc_to_its_own_track(app, tmp_path):
    root = tmp_path / "Library"
    root.mkdir()
    show = root / "A" / "top"
    lines = []
    for disc in (1, 2):
        p = show / f"CD{disc}" / "01.flac"
        _flac(p, ARTIST="A", DATE="1977-05-09", VENUE="V")
        md5 = f"{FLAC(str(p)).info.md5_signature:032x}"
        lines.append(f"CD{disc}/01.flac:{md5}")
    (show / "top.ffp").write_text("\n".join(lines) + "\n")

    app.config["LIBRARY_ROOT"] = str(root)
    run = BulkIngestRun(root=str(root), status="running")
    _db.session.add(run)
    _db.session.commit()
    bulk_ingest_run.discover(run)
    bulk_ingest_run.process(run, _never_stop)

    item = _db.session.query(BulkIngestItem).filter_by(run_id=run.id).first()
    assert item.status == "ingested"
    rec = _db.session.get(Recording, item.recording_id)
    by_disc = {t.disc_number: t for t in rec.tracks}
    assert by_disc[1].checksum_status == "match"
    assert by_disc[2].checksum_status == "match"


# ── R2-5: unpadded filenames sort naturally, matching the interactive path ──

def test_unpadded_filenames_ingest_in_natural_track_order(app, tmp_path):
    root = tmp_path / "Library"
    root.mkdir()
    for i in range(1, 11):
        _flac(root / "Unpadded" / f"t{i}.flac",
              ARTIST="A", DATE="1977-05-08", VENUE="V", TRACKNUMBER=str(i),
              TITLE=f"Song {i}")

    app.config["LIBRARY_ROOT"] = str(root)
    run = BulkIngestRun(root=str(root), status="running")
    _db.session.add(run)
    _db.session.commit()
    bulk_ingest_run.discover(run)
    bulk_ingest_run.process(run, _never_stop)

    item = _db.session.query(BulkIngestItem).filter_by(run_id=run.id).first()
    assert item.status == "ingested"
    rec = _db.session.get(Recording, item.recording_id)
    ordered = sorted(rec.tracks, key=lambda t: t.track_number)
    assert [t.title for t in ordered] == [f"Song {i}" for i in range(1, 11)]
    assert [t.file_path for t in ordered] == [f"t{i}.flac" for i in range(1, 11)]


def test_padded_filenames_unaffected_by_natural_sort(app, tmp_path):
    root = tmp_path / "Library"
    root.mkdir()
    for i in range(1, 11):
        _flac(root / "Padded" / f"t{i:02d}.flac",
              ARTIST="A", DATE="1977-05-08", VENUE="V", TRACKNUMBER=str(i),
              TITLE=f"Song {i}")

    app.config["LIBRARY_ROOT"] = str(root)
    run = BulkIngestRun(root=str(root), status="running")
    _db.session.add(run)
    _db.session.commit()
    bulk_ingest_run.discover(run)
    bulk_ingest_run.process(run, _never_stop)

    item = _db.session.query(BulkIngestItem).filter_by(run_id=run.id).first()
    rec = _db.session.get(Recording, item.recording_id)
    ordered = sorted(rec.tracks, key=lambda t: t.track_number)
    assert [t.title for t in ordered] == [f"Song {i}" for i in range(1, 11)]


# ── N5: a worker-level exception leaves the run paused, not running ────────

def test_worker_exception_leaves_run_paused_with_last_error(app, tmp_path):
    root = tmp_path / "Library"
    root.mkdir()
    app.config["LIBRARY_ROOT"] = str(root)

    def _boom(run):
        raise RuntimeError("simulated unmounted root")

    with mock.patch.object(bulk_ingest_run, "discover", _boom):
        run = bulk_ingest_run.start_run(str(root))
        deadline = time.time() + 5
        _db.session.expire_all()
        r = _db.session.get(BulkIngestRun, run.id)
        while r.status == "running" and time.time() < deadline:
            time.sleep(0.1)
            _db.session.expire_all()
            r = _db.session.get(BulkIngestRun, run.id)

    assert r.status == "paused"
    assert r.last_error and "simulated unmounted root" in r.last_error
    worker = bulk_ingest_run._ACTIVE_WORKERS.get(run.id)
    assert worker is None or not worker.is_alive()


# ── N6: a rejected review row reconciles to skipped, not left dangling ─────

def test_rejected_review_item_reconciles_to_skipped(app, tmp_path):
    root = tmp_path / "Library"
    root.mkdir()
    _flac(root / "NeedsDate" / "01.flac", ARTIST="A", VENUE="V")

    app.config["LIBRARY_ROOT"] = str(root)
    run = BulkIngestRun(root=str(root), status="running")
    _db.session.add(run)
    _db.session.commit()
    bulk_ingest_run.discover(run)
    bulk_ingest_run.process(run, _never_stop)

    item = _db.session.query(BulkIngestItem).filter_by(run_id=run.id).first()
    assert item.status == "review"

    from app.utils.quality_store import norm_path, TRIAGE_REJECTED
    staging = (_db.session.query(QualityAnalysis)
               .filter_by(folder_path=norm_path(str(root / "NeedsDate")))
               .first())
    staging.triage_status = TRIAGE_REJECTED
    _db.session.commit()

    n = bulk_ingest_run._reconcile_review_items()
    assert n == 1
    _db.session.refresh(item)
    assert item.status == "skipped"
    assert item.reason == "rejected"
