"""Bulk Value Apply "Clear disc numbers" (Ryan, 2026-10-13)."""
import json

from app.utils.resolve import clear_disc_numbers, tag_track_shortfall


def _scan():
    files = [{"filename": f"{i:02d}.mp3", "rel_path": f"{i:02d}.mp3", "disc_number": 17,
              "disc_track_number": i} for i in (1, 2)]
    tags = [{"filename": f["filename"], "rel_path": f["rel_path"],
             "raw": {"tracknumber": str(i + 1), "discnumber": "17", "disctotal": "20"}}
            for i, f in enumerate(files)]
    return {"sets_detected": True, "audio_files": files,
            "suggestions": {"from_tags": {"tracks": tags}}}


def test_clear_disc_numbers_forgets_every_disc_reading():
    scan = clear_disc_numbers(_scan())
    assert scan["sets_detected"] is False
    assert all(f["disc_number"] is None for f in scan["audio_files"])
    assert all("discnumber" not in t["raw"] for t in scan["suggestions"]["from_tags"]["tracks"])
    assert tag_track_shortfall(scan) is None


def test_applied_switch_is_stored_and_rereads_waiting_rows(app):
    from app.extensions import db
    from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
    from app.utils import bulk_ingest_run as bir
    from tests.test_studio_sharing import _login_as
    run = BulkIngestRun(root="/tmp/x", status="done"); db.session.add(run); db.session.flush()
    it = BulkIngestItem(run_id=run.id, rel_path="A", status="review", reason="incomplete_recording")
    db.session.add(it); db.session.commit()
    started = []
    import app.utils.bulk_ingest_run as m
    orig = m._start_worker
    m._start_worker = lambda: started.append(1)
    try:
        c = app.test_client(); _login_as(c)
        r = c.put(f"/api/bulk-ingest/runs/{run.id}/applied", json={"clear_disc_numbers": True})
        assert r.status_code == 200
    finally:
        m._start_worker = orig
    assert json.loads(run.applied_json) == {"clear_disc_numbers": True}
    assert it.status == "pending" and started
