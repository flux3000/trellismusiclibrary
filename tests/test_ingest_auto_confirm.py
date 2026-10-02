"""
tests/test_ingest_auto_confirm.py -- POST /api/ingest/auto-confirm is a
background job, not a synchronous call (Ingest Field Resolver spec v1,
Fix 1): Batch Import's sources live under IMPORT_DIR, outside
LIBRARY_ROOT, and an "ingested" verdict still runs the same copy/move
_do_confirm does for interactive /confirm -- slow enough on a real NAS/USB
import source to outrun the webview's fetch timeout. The route must
therefore return a job id and let the CALLER poll the existing
GET /api/ingest/confirm/<job_id> endpoint, exactly like /confirm does,
rather than block the request.
"""
import time

from app.extensions import db as _db
from app.models.user import User

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC


def _flac_with_tags(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for k, v in tags.items():
        audio[k] = v
    audio.save()


_INFO = '''Pat Metheny Group
June 14, 1979
Stars - Philadelphia, Pennsylvania, USA
Soundboard Recording
'''


def _out_of_root_show(tmp_path):
    """A show folder under its own "import" tree, deliberately NOT inside
    the "library" tree used as LIBRARY_ROOT -- the exact IMPORT_DIR vs.
    LIBRARY_ROOT split Batch Import sources live under in production."""
    show = tmp_path / "import" / "Pat Metheny Group - 1979-06-14 - Stars - Philadelphia, PA (SBD)"
    for i in range(1, 3):
        _flac_with_tags(show / f"{i:02d}.flac", ARTIST="Pat Metheny Group", DATE="1979-06-14")
    (show / "info.txt").write_text(_INFO)
    return show



def _login_as(client, username="admin"):
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None, f"no such user: {username}"
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return user


def _poll_until_done(client, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/ingest/confirm/{job_id}")
        assert resp.status_code == 200
        body = resp.get_json()
        if body["status"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never left 'running' within {timeout}s")


def test_auto_confirm_out_of_root_source_goes_through_the_job_path(app, tmp_path):
    library_root = tmp_path / "library"
    library_root.mkdir()
    app.config["LIBRARY_ROOT"] = str(library_root)

    show = _out_of_root_show(tmp_path)
    # Confirms the fixture itself is actually out-of-root, i.e. this test
    # exercises the scenario Fix 1 is about -- not a source that happened
    # to already live inside LIBRARY_ROOT.
    assert not str(show).startswith(str(library_root))

    client = app.test_client()
    _login_as(client)

    # 1. The route must NOT block on the copy: it returns a job id straight
    #    away, the same shape /api/ingest/confirm returns.
    resp = client.post("/api/ingest/auto-confirm", json={"path": str(show)})
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]
    assert job_id

    # 2. It is readable through the EXISTING confirm-job poll endpoint --
    #    no separate auto-confirm poll route.
    body = _poll_until_done(client, job_id)
    assert body["status"] == "done"

    # 3. The poller sees auto_confirm()'s own result dict, verdict and all,
    #    exactly as the (now-removed) synchronous response used to shape it.
    result = body["result"]
    assert result["status"] == "ingested"
    assert result["reasons"] == []
    assert result["format"] == "FLAC"
    assert result["result"]["recording_id"]

    # 4. And the copy this job ran for real actually landed inside
    #    LIBRARY_ROOT, proving this was a genuine out-of-root move, not a
    #    no-op because the source already lived in-root.
    moved = list(library_root.rglob("*.flac"))
    assert moved, "expected the show's files to have been copied into LIBRARY_ROOT"

    # 5. A second poll of the same job_id is gone -- confirm jobs are
    #    popped on their first terminal read, same as /confirm's.
    resp2 = client.get(f"/api/ingest/confirm/{job_id}")
    assert resp2.status_code == 404


def test_auto_confirm_review_verdict_is_also_readable_via_the_job_poller(app, tmp_path):
    """A non-"ingested" verdict (here: no date at all -> "review") must
    still reach the poller through the same job -- Fix 1 explicitly
    requires this, since auto_confirm() has four terminal outcomes and the
    job/poll pattern was built around _do_confirm's single "done"."""
    library_root = tmp_path / "library"
    library_root.mkdir()
    app.config["LIBRARY_ROOT"] = str(library_root)

    show = tmp_path / "import" / "Some Band - Somewhere"
    _flac_with_tags(show / "01.flac", ARTIST="Some Band")  # no DATE tag at all

    client = app.test_client()
    _login_as(client)
    resp = client.post("/api/ingest/auto-confirm", json={"path": str(show)})
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]

    body = _poll_until_done(client, job_id)
    assert body["status"] == "done"
    result = body["result"]
    assert result["status"] == "review"
    assert "needs_date" in result["reasons"]
    assert result["result"] is None

    # Nothing should have been copied for a review verdict.
    assert not list(library_root.rglob("*.flac"))
