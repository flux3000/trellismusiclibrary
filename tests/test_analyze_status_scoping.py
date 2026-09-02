"""
tests/test_analyze_status_scoping.py — the review queue only ever lists the
folders the scan that produced it actually resolved.

The bug (Ryan, 2026-09-02): a Pat Metheny show from several jobs ago that would
not leave the Review & Ingest list.  `qs.list_staging()` answers "every
unpromoted row recorded under this source dir", which is a SUPERSET of "what is
under this directory now" — staging rows deliberately outlive their folders so
`promote_to_recording` still has something to write to after a Move ingest.
The same show had been ingested from a different path, so the row under the
scanned parent was never marked promoted, and nothing else would ever clear it.

The job knows the real answer before a single byte is decoded, so
`analyze_status` filters its results to that.  These tests pin the filter, and
the one case that must NOT be filtered out.
"""

import os
import tempfile

import pytest

from app.api import quality as quality_api
from app.extensions import db as _db
from app.models.user import User
from app.utils import quality_store as qs
from app.utils.quality import score_recording

from tests.test_quality import _features


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


def _stage(path, source_dir):
    return qs.upsert_staging(path, source_dir=source_dir,
                             scored=score_recording(_features()),
                             features=_features())


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def job(app):
    """A finished job whose result set the test then polls for."""
    quality_api._QUALITY_JOBS["j1"] = {
        "status": "done", "total": 1, "done": 1,
        "current": None, "error": None, "ingested": [],
        "paths": [],
    }
    yield quality_api._QUALITY_JOBS["j1"]
    quality_api._QUALITY_JOBS.pop("j1", None)


def _poll(client, source_dir):
    r = client.get(f"/api/quality/analyze/j1?source_dir={source_dir}")
    assert r.status_code == 200
    return [row["folder_path"] for row in r.get_json()["results"]]


def test_ghost_row_from_an_earlier_job_is_not_listed(app, client, job):
    """A row under this source dir that THIS scan did not resolve stays out."""
    _stage("/src/this-scan", "/src")
    _stage("/src/pat-metheny-from-june", "/src")
    job["paths"] = ["/src/this-scan"]
    _login_as(client)

    assert _poll(client, "/src") == ["/src/this-scan"]


def test_rows_this_scan_resolved_are_all_listed(app, client, job):
    _stage("/src/a", "/src")
    _stage("/src/b", "/src")
    job["paths"] = ["/src/a", "/src/b"]
    _login_as(client)

    assert sorted(_poll(client, "/src")) == ["/src/a", "/src/b"]


def test_filter_matches_on_the_normalised_path(app, client, job):
    """
    Paths are compared through norm_path, not raw.  macOS hands back decomposed
    filenames while SQLite stores composed ones, and a filter that compared the
    bytes would silently drop every accented folder from the queue — the exact
    failure mode CONTEXT.md warns about, arriving as an empty list rather than
    an error.
    """
    import unicodedata
    accented = "/src/Lucía Trio"
    _stage(unicodedata.normalize("NFC", accented), "/src")
    job["paths"] = [unicodedata.normalize("NFD", accented)]
    _login_as(client)

    assert _poll(client, "/src") == [unicodedata.normalize("NFC", accented)]


def test_a_job_with_no_path_list_is_unfiltered(app, client, job):
    """
    Backward compatibility with a job started before this change and still in
    memory across a reload.  `paths` absent means "no opinion", which must read
    as the old behaviour rather than as an empty queue — an empty queue is
    indistinguishable from a broken scan, and that misdirection is what the
    fix exists to remove.
    """
    _stage("/src/a", "/src")
    job.pop("paths")
    _login_as(client)

    assert _poll(client, "/src") == ["/src/a"]
