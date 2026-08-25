"""
tests/test_triage_rescan.py — the triage list must survive a re-scan at a
different directory level.

Regression cover for the 2026-08-25 stall ("Review and Ingest just stalls out —
all of the recordings just stick at Analyzing"). Staging rows are keyed by
folder path but FETCHED by source_dir, and the analysis job's skip-if-current
path never repointed source_dir. Scanning an act folder after previously
scanning its parent therefore produced a job that reported "done" while
`list_staging()` returned nothing, so the UI's placeholder cards were never
replaced and no error was raised anywhere.

No audio is decoded here: every folder is already "current", which is precisely
the path under test.
"""

import os
import tempfile

import pytest

from config import Config
from app import create_app
from app.extensions import db as _db
from app.utils import quality_store as qs
from app.utils.quality import QUALITY_ANALYSIS_VERSION


PARENT = "/Volumes/music/Trellis/Download"
ACT    = PARENT + "/Danny Gatton"
SHOW   = ACT + "/Danny Gatton - 1979-01-25 - Cellar Door - Washington, DC"
SHOW2  = ACT + "/Danny Gatton - 1988-12-03 - Hunter College - New York, NY"

_SCORED = {"listening_quality": 73.2, "score_tone": 70.0, "score_noise": 75.0,
           "score_dynamics": 74.0, "technical_deduction": 0.0,
           "score_version": "3", "technical_issues": [], "flags": []}
_FEATURES = {"analysis_version": QUALITY_ANALYSIS_VERSION, "sampled": []}


@pytest.fixture()
def qapp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    class TestConfig(Config):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"
        TESTING = True
        DEV_MODE = False

    application = create_app(config_class=TestConfig)
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
    os.unlink(path)


def _seed(folder, source_dir):
    return qs.upsert_staging(folder, source_dir=source_dir,
                             name=os.path.basename(folder),
                             scored=_SCORED, features=_FEATURES)


def _run(app, source_dir, folders, reanalyze=False):
    """Run the real background worker body synchronously and return its job."""
    from app.api.quality import _run_quality_job, _QUALITY_JOBS

    job_id = "test-job"
    _QUALITY_JOBS[job_id] = {"status": "running", "total": len(folders),
                             "done": 0, "current": None, "error": None,
                             "ingested": []}
    _run_quality_job(job_id, app, source_dir, folders, reanalyze)
    return _QUALITY_JOBS.pop(job_id)


def test_rescan_at_a_deeper_level_still_returns_the_rows(qapp):
    """The stall itself: scan the parent, then scan the act folder."""
    _seed(SHOW, PARENT)
    _seed(SHOW2, PARENT)
    assert len(qs.list_staging(PARENT)) == 2

    job = _run(qapp, ACT, [SHOW, SHOW2])

    assert job["status"] == "done" and job["error"] is None
    rows = qs.list_staging(ACT)
    assert len(rows) == 2, "the scanned directory must return its own shows"
    assert {r.folder_path for r in rows} == {SHOW, SHOW2}
    # The rows MOVED — they belong to the directory most recently scanned,
    # exactly as a re-analysis (qs.upsert_staging) would have left them.
    assert qs.list_staging(PARENT) == []


def test_rescan_at_a_shallower_level_still_returns_the_rows(qapp):
    """And the other direction: scan the act folder, then its parent."""
    _seed(SHOW, ACT)
    job = _run(qapp, PARENT, [SHOW])

    assert job["status"] == "done"
    assert [r.folder_path for r in qs.list_staging(PARENT)] == [SHOW]


def test_rescanning_the_same_directory_writes_nothing(qapp):
    """
    The common case — re-opening the same folder — must not cost a commit per
    show. Guarded because _adopt_into_scan runs on EVERY skipped folder.
    """
    row = _seed(SHOW, PARENT)
    before = row.updated_at

    _run(qapp, PARENT, [SHOW])

    assert qs.get_staging(SHOW).updated_at == before
    assert [r.folder_path for r in qs.list_staging(PARENT)] == [SHOW]


def test_already_ingested_folders_are_reported_not_silently_dropped(qapp):
    """
    A promoted row is excluded from list_staging by design, so without this the
    client has a placeholder card it can never resolve. The job names them.
    """
    from app.models.user import User
    from app.models.performer import Performer
    from app.models.performance import Performance
    from app.models.recording import Recording

    perf = Performer(name="Danny Gatton")
    _db.session.add(perf)
    _db.session.flush()
    show = Performance(performer_id=perf.id, start_year=1979)
    _db.session.add(show)
    _db.session.flush()
    rec = Recording(performance_id=show.id, folder_path=SHOW)
    _db.session.add(rec)
    _db.session.flush()

    _seed(SHOW, PARENT)
    qs.promote_to_recording(SHOW, rec.id)
    assert qs.list_staging(PARENT) == []          # excluded, as designed

    job = _run(qapp, PARENT, [SHOW])

    assert job["status"] == "done"
    assert job["ingested"] == [SHOW], "the client must be told why there is no row"
