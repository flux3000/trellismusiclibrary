"""
tests/test_bulk_ingest_resume.py -- resuming a Bulk Ingest run after a
simulated crash (spec chunk 5b "Resume on boot").

Real files under a temp LIBRARY_ROOT. Rather than truly killing a thread
mid-flight, this simulates the moment a crash leaves an BulkIngestItem
'in_progress' with unknown outcome: run process() far enough that two items
are finished, hand-set the next pending item to 'in_progress' (exactly what
an interrupted worker would have left behind), reset it via
reset_in_progress() (what resume_on_boot() does before restarting the
worker), then let process() finish the run.
"""

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from app.extensions import db as _db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.recording import Recording
from app.utils import bulk_ingest_run


def _flac(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    # A real-world FLAC always carries TRACKNUMBER; Track.track_number is
    # NOT NULL, so a synthetic fixture needs one too unless the test
    # supplies its own.
    tags.setdefault("TRACKNUMBER", "1")
    for k, v in tags.items():
        audio[k] = v
    audio.save()


def _finished_count(run_id):
    return (_db.session.query(BulkIngestItem)
            .filter(BulkIngestItem.run_id == run_id,
                    BulkIngestItem.status != "pending",
                    BulkIngestItem.status != "in_progress")
            .count())


def test_crash_mid_item_is_resumed_without_duplicating(app, tmp_path):
    root = tmp_path / "Library"
    root.mkdir()
    for i, (artist, date) in enumerate([
        ("Grateful Dead", "1977-05-08"),
        ("Phish", "1995-07-14"),
        ("Bela Fleck", "1998-03-01"),
    ], start=1):
        _flac(root / f"Show{i}" / "01.flac", ARTIST=artist, DATE=date,
              VENUE="Some Hall")

    app.config["LIBRARY_ROOT"] = str(root)

    run = BulkIngestRun(root=str(root), status="running")
    _db.session.add(run)
    _db.session.commit()
    bulk_ingest_run.discover(run)

    assert (_db.session.query(BulkIngestItem)
            .filter_by(run_id=run.id).count()) == 3

    # Stop as soon as two items have finished -- the third is never even
    # picked up by this first process() call.
    def _stop_after_two():
        return _finished_count(run.id) >= 2

    bulk_ingest_run.process(run, _stop_after_two)
    assert _finished_count(run.id) == 2
    run = _db.session.get(BulkIngestRun, run.id)
    assert run.status == "running"  # not done -- one item never touched

    # Simulate the crash: the worker had just picked up the third item and
    # marked it in_progress when the process died.
    third = (_db.session.query(BulkIngestItem)
            .filter_by(run_id=run.id, status="pending").first())
    assert third is not None
    third.status = "in_progress"
    _db.session.commit()

    # resume_on_boot()'s reset step, applied directly (no live app/thread
    # needed for this).
    bulk_ingest_run.reset_in_progress(run)
    reset_item = _db.session.get(BulkIngestItem, third.id)
    assert reset_item.status == "pending"

    def _never_stop():
        return False

    bulk_ingest_run.process(run, _never_stop)

    run = _db.session.get(BulkIngestRun, run.id)
    assert run.status == "done"

    items = _db.session.query(BulkIngestItem).filter_by(run_id=run.id).all()
    assert all(it.status == "ingested" for it in items)
    recording_ids = {it.recording_id for it in items}
    assert len(recording_ids) == 3  # no duplicate Recording from the retry

    assert _db.session.query(Recording).count() == \
        1 + 3  # the conftest seed recording, plus these three
