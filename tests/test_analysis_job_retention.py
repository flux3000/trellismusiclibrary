"""
tests/test_analysis_job_retention.py — a finished analysis job stays readable.

The bug (Ryan, 2026-09-03, screenshot; first reported 2026-08-29 and not
reproduced then): a bulk scan came back with a red "unknown job" banner and
eight rows reading "Analysis failed".  Reprocessing the very same directory
filed every one of them, which is what proves the folders were never the
problem.

`analyze_status` popped the job from `_QUALITY_JOBS` the instant a terminal
status was read, so a SECOND poll — ordinary, not exceptional: two poll loops
can overlap for a tick, a request can be retried, a slow response overtaken —
got a 404.  The client reads that as "the job died", and marks every folder the
run had not reached yet as failed.  A completed scan presenting as a page of
failures is the worst shape available here: it accuses the data.

Finished jobs are retained for a window now.  These tests pin that, and pin the
one thing that must NOT change with it: an unknown id is still a 404.
"""

import time

import pytest

from app.api import quality as quality_api
from app.extensions import db as _db
from app.models.user import User


def _login_as(client, username="admin"):
    from app.extensions import login_manager
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        gen = getattr(login_manager, "_session_identifier_generator", None)
        if callable(gen):
            try:
                sess["_id"] = gen()
            except Exception:
                pass


@pytest.fixture()
def client(app):
    c = app.test_client()
    _login_as(c)
    return c


@pytest.fixture()
def finished_job(app):
    quality_api._QUALITY_JOBS["fin"] = {
        "status": "done", "total": 3, "done": 3,
        "current": None, "error": None, "ingested": [], "paths": [],
    }
    yield quality_api._QUALITY_JOBS.get("fin")
    quality_api._QUALITY_JOBS.pop("fin", None)


def test_a_finished_job_survives_being_read(app, client, finished_job):
    """The whole bug in one assertion: poll twice, get the same answer twice."""
    first = client.get("/api/quality/analyze/fin?source_dir=/src")
    assert first.status_code == 200
    assert first.get_json()["status"] == "done"

    second = client.get("/api/quality/analyze/fin?source_dir=/src")
    assert second.status_code == 200, "the second poll got a 404 — this is the bug"
    assert second.get_json()["status"] == "done"


def test_an_errored_job_also_survives(app, client):
    """
    An error the client failed to read is worse than one it read twice: the
    real reason gets replaced by "unknown job", which explains nothing.
    """
    quality_api._QUALITY_JOBS["bad"] = {
        "status": "error", "total": 1, "done": 0, "current": None,
        "error": "the disk went away", "ingested": [], "paths": [],
    }
    try:
        client.get("/api/quality/analyze/bad?source_dir=/src")
        again = client.get("/api/quality/analyze/bad?source_dir=/src")
        assert again.status_code == 200
        assert again.get_json()["error"] == "the disk went away"
    finally:
        quality_api._QUALITY_JOBS.pop("bad", None)


def test_an_id_that_never_existed_is_still_404(app, client):
    """Retention must not turn a genuinely unknown id into a silent success."""
    assert client.get("/api/quality/analyze/nope").status_code == 404


def test_the_retention_clock_starts_at_the_first_read(app, client, finished_job):
    """
    Not when the worker finished. The window is "how long after somebody
    looked", because that is the interval a duplicate or retried poll lands in.
    """
    assert finished_job.get("finished_at") is None
    client.get("/api/quality/analyze/fin?source_dir=/src")
    assert quality_api._QUALITY_JOBS["fin"]["finished_at"] is not None


def test_finished_jobs_are_swept_once_the_window_passes(app, client, finished_job):
    client.get("/api/quality/analyze/fin?source_dir=/src")
    # Backdate past the window, then poke the sweeper via any status call.
    quality_api._QUALITY_JOBS["fin"]["finished_at"] = (
        time.time() - quality_api._JOB_RETAIN_SECONDS - 5)
    client.get("/api/quality/analyze/nope")
    assert "fin" not in quality_api._QUALITY_JOBS


def test_a_running_job_is_never_swept(app, client):
    """
    `finished_at` is the only thing that makes a job collectable. A long scan
    that outlives the retention window must not have its own job reaped out
    from under it — that would recreate the bug on exactly the runs where it
    hurts most.
    """
    quality_api._QUALITY_JOBS["long"] = {
        "status": "running", "total": 500, "done": 1, "current": "x",
        "error": None, "ingested": [], "paths": [],
    }
    try:
        quality_api._sweep_finished_jobs()
        assert "long" in quality_api._QUALITY_JOBS
    finally:
        quality_api._QUALITY_JOBS.pop("long", None)


def test_the_kept_set_is_capped(app):
    """A session that starts jobs faster than the clock expires them still bounds."""
    ids = []
    try:
        for i in range(quality_api._JOB_RETAIN_MAX + 15):
            jid = f"cap{i}"
            ids.append(jid)
            quality_api._QUALITY_JOBS[jid] = {
                "status": "done", "total": 1, "done": 1, "current": None,
                "error": None, "ingested": [], "paths": [],
                "finished_at": time.time() + i,   # all inside the window
            }
        quality_api._sweep_finished_jobs()
        kept = [j for j in ids if j in quality_api._QUALITY_JOBS]
        assert len(kept) <= quality_api._JOB_RETAIN_MAX
        # The ones kept are the NEWEST — an old job nobody is polling is the
        # right thing to lose.
        assert ids[-1] in quality_api._QUALITY_JOBS
    finally:
        for jid in ids:
            quality_api._QUALITY_JOBS.pop(jid, None)
