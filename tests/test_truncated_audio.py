"""A stub MP3 (tag plus one frame, like a dead download) reads as truncated;
the queue row says so instead of the reader's raw error."""
from app.utils.truncated_audio import is_truncated, truncated_files


def _stub(path, audio_bytes=1045):
    tag = 2281
    s = bytes([(tag >> 21) & 127, (tag >> 14) & 127, (tag >> 7) & 127, tag & 127])
    path.write_bytes(b"ID3\x02\x00\x00" + s + b"\x00" * tag
                     + b"\xff\xfb\xe2\x00" + b"\x00" * (audio_bytes - 4))


def test_stub_mp3_is_truncated(tmp_path):
    _stub(tmp_path / "01 a.mp3")
    assert is_truncated(str(tmp_path / "01 a.mp3"))
    assert truncated_files(str(tmp_path)) == ["01 a.mp3"]


def test_unreadable_but_long_is_not_called_truncated(tmp_path):
    _stub(tmp_path / "02 b.mp3", audio_bytes=200_000)   # five seconds at 320
    assert not is_truncated(str(tmp_path / "02 b.mp3"))


def test_auto_confirm_reports_truncated_file(app, tmp_path):
    from app.api.ingest import auto_confirm
    d = tmp_path / "Perpetual Motion"; d.mkdir()
    _stub(d / "01 a.mp3")
    out = auto_confirm(str(d), None)
    assert out["status"] == "failed"
    assert out["reasons"] == ["truncated_file"] and out["detail"] == "01 a.mp3"


def test_boot_pass_relabels_old_unreadable_rows(app, tmp_path):
    from app.extensions import db
    from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
    from app.utils.bulk_ingest_run import relabel_truncated_items
    d = tmp_path / "PM"; d.mkdir()
    _stub(d / "01 a.mp3")
    run = BulkIngestRun(root=str(tmp_path)); db.session.add(run); db.session.flush()
    it = BulkIngestItem(run_id=run.id, rel_path="PM", status="failed", reason="unreadable",
                        detail="can't sync to MPEG frame")
    db.session.add(it); db.session.commit()
    relabel_truncated_items()
    assert (it.reason, it.detail) == ("truncated_file", "01 a.mp3")


def test_failed_row_cannot_be_moved_only_trashed(app, tmp_path, monkeypatch):
    from app.extensions import db
    from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
    from app.api import quality
    run = BulkIngestRun(root=str(tmp_path)); db.session.add(run); db.session.flush()
    it = BulkIngestItem(run_id=run.id, rel_path="PM", status="failed", reason="truncated_file")
    db.session.add(it); db.session.commit()
    monkeypatch.setattr(quality, "move_folder_to_triage", lambda p, d, **k: ({"moved_to": "/x"}, 200))
    from tests.test_studio_sharing import _login_as
    c = app.test_client(); _login_as(c)
    r = c.post(f"/api/bulk-ingest/items/{it.id}/move", json={"dest": "workshop"})
    # A failed row has no recording: its only action is Trash (Move was removed from it).
    assert r.status_code == 409
    assert db.session.get(BulkIngestItem, it.id).status == "failed"


def test_queue_row_to_trash(app, tmp_path, monkeypatch):
    from app.extensions import db
    from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
    from app.utils import trash
    d = tmp_path / "PM"; d.mkdir()
    app.config["IMPORT_ROOTS"] = list(app.config.get("IMPORT_ROOTS") or []) + [str(tmp_path)]
    run = BulkIngestRun(root=str(tmp_path)); db.session.add(run); db.session.flush()
    it = BulkIngestItem(run_id=run.id, rel_path="PM", status="failed", reason="truncated_file")
    db.session.add(it); db.session.commit()
    seen = []
    monkeypatch.setattr(trash, "move_to_trash", lambda p: seen.append(p))
    from tests.test_studio_sharing import _login_as
    c = app.test_client(); _login_as(c)
    r = c.post(f"/api/bulk-ingest/items/{it.id}/move", json={"dest": "trash"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "moved" and seen == [str(d.resolve())]


def test_trash_takes_an_emptied_artist_folder_but_not_the_run_root(app, tmp_path, monkeypatch):
    import shutil
    from app.api.quality import trash_folder_path
    from app.utils import trash
    root = tmp_path / "src"; album = root / "Artist" / "Album"; album.mkdir(parents=True)
    (root / "Artist" / ".DS_Store").write_bytes(b"")
    app.config["IMPORT_ROOTS"] = [str(tmp_path)]
    seen = []

    def fake(p):
        seen.append(p); shutil.rmtree(p)
    monkeypatch.setattr(trash, "move_to_trash", fake)
    payload, status = trash_folder_path(str(album), stop_at=str(root))
    assert status == 200
    assert seen == [str(album.resolve()), str((root / "Artist").resolve())]
    assert root.exists()


def test_gone_folder_reads_folder_missing(app, tmp_path):
    from app.api.ingest import auto_confirm
    out = auto_confirm(str(tmp_path / "gone"), None)
    assert out["status"] == "failed" and out["reasons"] == ["folder_missing"]


def test_form_import_marks_the_queue_row_ingested(app, tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from app.extensions import db
    from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
    from app.utils import bulk_ingest_run as bir
    d = tmp_path / "A" / "Album"; d.mkdir(parents=True)
    run = BulkIngestRun(root=str(tmp_path)); db.session.add(run); db.session.flush()
    it = BulkIngestItem(run_id=run.id, rel_path="A/Album", status="ready", kind="studio")
    db.session.add(it); db.session.commit()
    monkeypatch.setattr(bir, "listed_runs", lambda: [run])
    from tests.test_studio_sharing import _recording
    rec = _recording("Album", kind="studio")
    assert bir.mark_folder_ingested(str(d), rec) == 1
    assert (it.status, it.recording_id) == ("ingested", rec.id)


def test_move_to_workshop_trashes_an_emptied_artist_folder(app, tmp_path, monkeypatch):
    import shutil
    from app.api.quality import move_folder_to_triage
    from app.utils import trash
    root = tmp_path / "src"; album = root / "Artist" / "Album"; album.mkdir(parents=True)
    (album / "01.mp3").write_bytes(b"x")
    ws = tmp_path / "Workshop"; ws.mkdir()
    app.config["IMPORT_ROOTS"] = [str(tmp_path)]
    app.config["TRIAGE_DIRS"] = {"workshop": str(ws)}
    seen = []
    monkeypatch.setattr(trash, "move_to_trash", lambda p: (seen.append(p), shutil.rmtree(p)))
    payload, status = move_folder_to_triage(str(album), "workshop", stop_at=str(root))
    assert status == 200, payload
    assert (ws / "Album" / "01.mp3").exists()
    assert seen == [str((root / "Artist").resolve())] and root.exists()
