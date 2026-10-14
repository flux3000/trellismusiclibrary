"""Backfill fills only empty roles, from the stored info file; dry run saves nothing."""
from app.extensions import db
from app.utils.engineer_backfill import backfill_engineers
from tests.test_studio_sharing import _recording


def test_backfill_from_info_file(app):
    rec = _recording("Bf")
    rec.info_file_content = "Taped by Bornfree\nTransfer: Charlie Miller\n"
    db.session.commit()
    rows = backfill_engineers(apply=False, read_tags=False)
    mine = sorted((r["role"], r["name"]) for r in rows if r["id"] == rec.id)
    assert mine == [("recording", "Bornfree"), ("transfer", "Charlie Miller")]
    assert rec.recording_engineer_id is None                     # preview wrote nothing
    backfill_engineers(apply=True, read_tags=False)
    rec = db.session.get(type(rec), rec.id)
    assert rec.recording_engineer.name == "Bornfree" and rec.transfer_engineer.name == "Charlie Miller"
    rows = backfill_engineers(apply=False, read_tags=False)
    assert not [r for r in rows if r["id"] == rec.id]           # nothing left to fill


def test_backfill_skips_studio(app):
    rec = _recording("Al", kind="studio")
    rec.info_file_content = "Recorded by Rudy Van Gelder"
    db.session.commit()
    assert not [r for r in backfill_engineers(read_tags=False) if r["id"] == rec.id]
