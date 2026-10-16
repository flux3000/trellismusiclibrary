"""app/utils/recording_delete.py -- remove a recording's rows (never its files).

Used by Reset Queue and Trash. It follows the same bottom-up order as the library's
DELETE /api/recordings/<id>, then runs the conservative orphan cleanup
(app.utils.orphans) instead of the library's blunter prune.
"""
import shutil

from app.extensions import db


def delete_recording_rows(rec_id):
    """Delete one recording with its tracks (and their analysis and play logs),
    fingerprints, events, collection links and image rows, and unlink every Queue
    row that pointed at it. Does not commit. Returns the entity ids it pointed at
    (orphans.collect_ids), for the cleanup."""
    from app.models.bulk_ingest import BulkIngestItem
    from app.models.collection import CollectionRecording
    from app.models.quality import QualityAnalysis, RecordingQuality
    from app.models.recording import Recording, RecordingFingerprint
    from app.models.recording_event import RecordingEvent
    from app.models.recording_image import RecordingImage
    from app.api.recordings import _delete_tracks_of_recording
    from app.utils import entity_images as ei
    from app.utils.orphans import collect_ids

    rec = db.session.get(Recording, rec_id)
    if rec is None:
        return None
    ids = collect_ids(rec)
    _delete_tracks_of_recording(rec_id)
    q = db.session.query
    q(RecordingFingerprint).filter_by(recording_id=rec_id).delete(synchronize_session=False)
    q(RecordingEvent).filter_by(recording_id=rec_id).delete(synchronize_session=False)
    q(CollectionRecording).filter_by(recording_id=rec_id).delete(synchronize_session=False)
    q(RecordingImage).filter_by(recording_id=rec_id).delete(synchronize_session=False)
    q(RecordingQuality).filter_by(recording_id=rec_id).delete(synchronize_session=False)
    q(QualityAnalysis).filter_by(recording_id=rec_id).update({"recording_id": None},
                                                             synchronize_session=False)
    q(BulkIngestItem).filter_by(recording_id=rec_id).update({"recording_id": None},
                                                            synchronize_session=False)
    q(BulkIngestItem).filter_by(duplicate_of=rec_id).update({"duplicate_of": None},
                                                            synchronize_session=False)
    db.session.expire_all()
    db.session.query(Recording).filter_by(id=rec_id).delete(synchronize_session=False)
    db.session.flush()
    try:                                       # image files live under DATA_DIR, not in a table
        images_dir = ei.image_dir("recordings", str(rec_id))
        if images_dir.is_dir():
            shutil.rmtree(images_dir)
    except OSError:
        pass
    return ids


def remove_recording(rec_id):
    """delete_recording_rows, then the orphan cleanup. Does not commit. Returns
    True when a row was removed."""
    from app.utils import orphans
    ids = delete_recording_rows(rec_id)
    if ids is None:
        return False
    orphans.cleanup(ids)
    return True
