"""
tests/test_bulk_ingest_reset.py -- POST /api/bulk-ingest/runs/<id>/reset
(Reset Queue): drops every not-yet-imported item, keeps imported ones, closes
the run, and never touches files.
"""

import pytest

from app.extensions import db as _db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.user import User
from app.utils import bulk_ingest_run as bir


@pytest.fixture()
def api(app, monkeypatch, tmp_path):
    app.config["LOGIN_DISABLED"] = True
    app.config["LIBRARY_ROOT"] = str(tmp_path)
    monkeypatch.setattr("app.utils.bulk_ingest_run._start_worker", lambda *a, **k: None)
    return app.test_client()


def _login_as(client, username):
    user = _db.session.query(User).filter_by(username=username).first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _run_with_items(api):
    _login_as(api, "admin")
    run_id = api.post("/api/bulk-ingest/start").get_json()["id"]
    for name, status in [("A", "ingested"), ("B", "moved"), ("C", "ready"),
                         ("D", "review"), ("E", "pending"), ("F", "failed"),
                         ("G", "skipped")]:
        _db.session.add(BulkIngestItem(run_id=run_id, rel_path=name, status=status))
    _db.session.commit()
    return run_id


def _statuses(run_id):
    return {i.rel_path: i.status for i in
            _db.session.query(BulkIngestItem).filter_by(run_id=run_id).all()}


def test_reset_removes_queue_keeps_imported_and_unlists_run(api):
    run_id = _run_with_items(api)
    assert any(r["id"] == run_id for r in api.get("/api/bulk-ingest/runs").get_json()["runs"])

    r = api.post(f"/api/bulk-ingest/runs/{run_id}/reset")
    assert r.status_code == 200
    assert r.get_json()["removed"] == 5
    assert _statuses(run_id) == {"A": "ingested", "B": "moved"}
    assert _db.session.get(BulkIngestRun, run_id).status == "done"
    assert api.get("/api/bulk-ingest/runs").get_json()["runs"] == []


def test_reset_is_idempotent(api):
    run_id = _run_with_items(api)
    api.post(f"/api/bulk-ingest/runs/{run_id}/reset")
    r = api.post(f"/api/bulk-ingest/runs/{run_id}/reset")
    assert r.status_code == 200 and r.get_json()["removed"] == 0
    assert _statuses(run_id) == {"A": "ingested", "B": "moved"}


def test_reset_leaves_in_progress_item_alone(api):
    run_id = _run_with_items(api)
    _db.session.add(BulkIngestItem(run_id=run_id, rel_path="H", status="in_progress"))
    _db.session.commit()
    api.post(f"/api/bulk-ingest/runs/{run_id}/reset")
    assert _statuses(run_id)["H"] == "in_progress"


def test_reset_paused_run_and_new_scan_starts_fresh(api):
    run_id = _run_with_items(api)
    api.post(f"/api/bulk-ingest/{run_id}/pause")
    api.post(f"/api/bulk-ingest/runs/{run_id}/reset")
    new_id = api.post("/api/bulk-ingest/start").get_json()["id"]
    assert new_id != run_id


def test_reset_unknown_run_404(api):
    _login_as(api, "admin")
    assert api.post("/api/bulk-ingest/runs/9999/reset").status_code == 404


def test_reset_refuses_non_admin(api):
    run_id = _run_with_items(api)
    _db.session.add(User(username="viewer", role="viewer", is_active=True, password_hash="x"))
    _db.session.commit()
    _login_as(api, "viewer")
    assert api.post(f"/api/bulk-ingest/runs/{run_id}/reset").status_code == 403
    assert len(_statuses(run_id)) == 7
