"""
tests/test_bulk_ingest_dedup.py -- re-run discovery and FFP/ST5 duplicate
detection (spec chunk 5b "Dedup flag" / "Re-run").
"""

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from app.extensions import db as _db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.recording import Recording
from app.utils import bulk_ingest_run


def _never_stop():
    return False


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


# A real 32-hex-char token is all parse_checksum_file() needs; it does not
# recompute anything to validate it, so a made-up hash is fine for exercising
# the match/dedup logic.
_SHARED_HASH = "abcd1234abcd1234abcd1234abcd1234"


def _ffp(path, hexhash, filename="01.flac"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{hexhash} *{filename}\n")


def test_rerun_skips_already_ingested_and_reoffers_review(app, tmp_path):
    root = tmp_path / "Library"
    root.mkdir()
    _flac(root / "ShowA" / "01.flac", ARTIST="Grateful Dead", DATE="1977-05-08",
          VENUE="Barton Hall")
    _flac(root / "ShowReview" / "01.flac", ARTIST="Phish")  # needs_date
    app.config["LIBRARY_ROOT"] = str(root)

    run1 = BulkIngestRun(root=str(root), status="running")
    _db.session.add(run1)
    _db.session.commit()
    bulk_ingest_run.discover(run1)
    bulk_ingest_run.process(run1, _never_stop)

    run1 = _db.session.get(BulkIngestRun, run1.id)
    assert run1.status == "done"
    item_a = (_db.session.query(BulkIngestItem)
             .filter_by(run_id=run1.id, rel_path="ShowA").first())
    assert item_a.status == "ingested"
    item_review = (_db.session.query(BulkIngestItem)
                  .filter_by(run_id=run1.id, rel_path="ShowReview").first())
    assert item_review.status == "review"

    # A new folder appears between runs.
    _flac(root / "ShowB" / "01.flac", ARTIST="Phish", DATE="1995-07-14",
          VENUE="The Gorge")

    run2 = BulkIngestRun(root=str(root), status="running")
    _db.session.add(run2)
    _db.session.commit()
    bulk_ingest_run.discover(run2)

    run2_rels = {it.rel_path for it in
                _db.session.query(BulkIngestItem).filter_by(run_id=run2.id).all()}
    # ShowA (already ingested in run1) is never re-offered; ShowReview (still
    # needing a decision) and the new ShowB both are.
    assert run2_rels == {"ShowReview", "ShowB"}


def test_ffp_hash_match_sets_duplicate_of_and_still_ingests(app, tmp_path):
    root = tmp_path / "Library"
    root.mkdir()
    _flac(root / "ShowA" / "01.flac", ARTIST="Grateful Dead", DATE="1977-05-08",
          VENUE="Barton Hall")
    _ffp(root / "ShowA" / "checksum.ffp", _SHARED_HASH)

    # A second, differently-named folder of the SAME show, same FFP hash --
    # a duplicate transfer of the same recording.
    _flac(root / "ShowA (2)" / "01.flac", ARTIST="Grateful Dead", DATE="1977-05-08",
          VENUE="Barton Hall")
    _ffp(root / "ShowA (2)" / "checksum.ffp", _SHARED_HASH)
    app.config["LIBRARY_ROOT"] = str(root)

    run = BulkIngestRun(root=str(root), status="running")
    _db.session.add(run)
    _db.session.commit()
    bulk_ingest_run.discover(run)
    bulk_ingest_run.process(run, _never_stop)

    item_a  = (_db.session.query(BulkIngestItem)
              .filter_by(run_id=run.id, rel_path="ShowA").first())
    item_a2 = (_db.session.query(BulkIngestItem)
              .filter_by(run_id=run.id, rel_path="ShowA (2)").first())

    # Both still get ingested (dedup flags, it does not block) -- and the
    # second one in file order points at the first.
    assert item_a.status == "ingested"
    assert item_a2.status == "ingested"
    assert item_a.recording_id != item_a2.recording_id
    assert item_a.duplicate_of is None
    assert item_a2.duplicate_of == item_a.recording_id

    assert _db.session.query(Recording).count() == 1 + 2  # seed + these two
