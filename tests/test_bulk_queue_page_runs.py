"""
tests/test_bulk_queue_page_runs.py -- the Queue page lists every run Reset Queue
has not cleared, not just the running/paused ones.
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
    c = app.test_client()
    user = _db.session.query(User).filter_by(username="admin").first()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _finished_run(root, items):
    run = BulkIngestRun(root=root, status="done", mode="hold")
    _db.session.add(run)
    _db.session.commit()
    for name, status in items:
        _db.session.add(BulkIngestItem(run_id=run.id, rel_path=name, status=status,
                                       kind="live"))
    _db.session.commit()
    return run


def _rel(client, status):
    return sorted(i["rel_path"] for i in
                  client.get(f"/api/bulk-ingest/queue/items?status={status}").get_json()["items"])


def test_finished_run_with_only_failed_and_skipped_stays_on_page(api, tmp_path):
    old = _finished_run(str(tmp_path / "a"), [("F1", "failed"), ("S1", "skipped"), ("I1", "ingested")])
    assert old not in bir.listed_runs()          # not a working run any more
    # a second source starts and is running
    new = BulkIngestRun(root=str(tmp_path / "b"), status="running", mode="hold")
    _db.session.add(new)
    _db.session.commit()
    _db.session.add(BulkIngestItem(run_id=new.id, rel_path="R1", status="ready"))
    _db.session.add(BulkIngestItem(run_id=new.id, rel_path="S2", status="skipped"))
    _db.session.commit()

    # The older source keeps its failed row; its skipped row drops off the page.
    # Only the newest source shows skipped rows (Ryan, 2026-10-15).
    assert _rel(api, "queue") == ["F1", "R1", "S2"]
    assert _rel(api, "live") == ["I1"]
    cur = api.get("/api/bulk-ingest/current").get_json()
    assert cur["counts"]["failed"] == 1 and cur["counts"]["skipped"] == 1
    assert cur["counts"]["ingested"] == 1 and cur["counts"]["ready"] == 1


def test_reset_queue_is_the_only_thing_that_clears(api, tmp_path):
    _finished_run(str(tmp_path / "a"), [("F1", "failed"), ("I1", "ingested")])
    _finished_run(str(tmp_path / "b"), [("S2", "skipped"), ("I2", "ingested")])
    assert _rel(api, "queue") == ["F1", "S2"]
    assert _rel(api, "live") == ["I1", "I2"]
    api.post("/api/bulk-ingest/queue/reset")
    assert _rel(api, "queue") == []
    assert _rel(api, "live") == []


def test_queued_paths_covers_finished_runs_leftovers(api, tmp_path):
    old = _finished_run(str(tmp_path / "a"), [("F1", "failed"), ("I1", "ingested")])
    paths = bir.queued_paths()
    assert any(p.endswith("F1") for p in paths)
    assert not any(p.endswith("I1") for p in paths)
    assert bir.queued_paths(exclude_run_id=old.id) == set()


def test_worker_scope_unchanged(api, tmp_path):
    old = _finished_run(str(tmp_path / "a"), [("F1", "failed")])
    assert bir.listed_runs() == [] and bir.queue_runs() == []
    assert [r.id for r in bir.page_runs()] == [old.id]
