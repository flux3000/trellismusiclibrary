"""
tests/test_bulk_ingest_models.py — Bulk Ingest models (spec chunk 2).

BulkIngestRun / BulkIngestItem persistence, and the new Recording.kind column
they travel alongside (live vs studio).
"""

from io import BytesIO

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.extensions import db as _db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.recording import Recording


@pytest.fixture()
def api(app):
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


def _login_as(client, username="admin"):
    from app.models.user import User
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_create_all_creates_both_tables(app):
    names = set(inspect(_db.engine).get_table_names())
    assert "bulk_ingest_run" in names
    assert "bulk_ingest_item" in names


def test_unique_run_rel_path_enforced(app):
    run = BulkIngestRun(root="/tmp/somelib")
    _db.session.add(run)
    _db.session.flush()

    _db.session.add(BulkIngestItem(run_id=run.id, rel_path="Show One"))
    _db.session.commit()

    _db.session.add(BulkIngestItem(run_id=run.id, rel_path="Show One"))
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_deleting_run_cascades_items(app):
    run = BulkIngestRun(root="/tmp/somelib")
    _db.session.add(run)
    _db.session.flush()
    item = BulkIngestItem(run_id=run.id, rel_path="Show Two")
    _db.session.add(item)
    _db.session.commit()
    item_id = item.id

    _db.session.delete(run)
    _db.session.commit()

    assert _db.session.get(BulkIngestItem, item_id) is None


def test_recording_kind_defaults_live(app):
    rec = _db.session.query(Recording).first()
    assert rec.kind == "live"


def test_put_accepts_studio_rejects_invalid_serialized_in_get(api, app, seeded_ids):
    _login_as(api)
    rid = seeded_ids["recording_id"]

    r = api.put(f"/api/recordings/{rid}", json={"kind": "studio"})
    assert r.status_code == 200

    r = api.get(f"/api/recordings/{rid}")
    assert r.get_json()["kind"] == "studio"

    r = api.put(f"/api/recordings/{rid}", json={"kind": "album"})
    assert r.status_code == 400
