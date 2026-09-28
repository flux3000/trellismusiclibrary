"""
models/recording_image.py — multiple images per Recording, one designated
primary (Studio Records spec v1, chunk 4).

Modeled on app/models/artist_image.py: same columns, same
"PRIMARY IS ENFORCED IN APP LOGIC, not a DB constraint" rule, same
set_primary()/primary_for() helpers from app/utils/entity_images.py. The one
real difference is `source_ref`, widened to String(512) here -- a
folder-relative image path can run deeper than a Commons filename ever
does (e.g. a nested disc subfolder), and `embedded:<track file_path>` adds
its own prefix on top of that.

Files live at DATA_DIR/images/recordings/<recording id>/<filename>, never
under LIBRARY_ROOT (Trellis never writes into a collector's folder) -- see
image_dir()'s "recordings" branch in app/utils/entity_images.py.
"""

from datetime import datetime, timezone
from app.extensions import db


class RecordingImage(db.Model):
    __tablename__ = "recording_image"

    # The hook every helper in app/utils/entity_images.py keys off.
    __parent_fk__ = "recording_id"

    id           = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer,
                             db.ForeignKey("recording.id", ondelete="CASCADE"),
                             nullable=False, index=True)

    # Basename only, inside the recording's image dir -- never a full path
    # (recording paths are deliberately never exposed to the frontend).
    filename   = db.Column(db.String(255), nullable=False)
    ext        = db.Column(db.String(8), nullable=False)

    is_primary = db.Column(db.Boolean, nullable=False, default=False,
                           server_default="0")
    sort_order = db.Column(db.Integer, nullable=False, default=0,
                           server_default="0")

    # 'folder' (found in the recording folder at ingest), 'embedded'
    # (extracted from a FLAC PICTURE block or ID3 APIC frame), or 'upload'
    # (added by hand on View Recording, same as artist/venue photos).
    origin     = db.Column(db.String(24), nullable=False, default="upload",
                           server_default="upload")
    caption    = db.Column(db.String(255), nullable=True)
    credit     = db.Column(db.String(255), nullable=True)

    # DEDUPLICATION key. For origin='folder' this is the image's path
    # relative to the recording folder ("cover.jpg", "CD1/folder.jpg"); for
    # origin='embedded' it's "embedded:<track file_path>". Re-ingesting the
    # same folder must not duplicate an image already stored, and comparing
    # bytes would be a guess where the source location is exact. Wider than
    # ArtistImage.source_ref (String(255)) because a folder-relative path can
    # run deeper than a Commons filename, and the "embedded:" prefix adds to
    # that. NULL for uploads.
    source_ref = db.Column(db.String(512), nullable=True)

    created_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(timezone.utc))

    recording = db.relationship("Recording", back_populates="images")

    def __repr__(self):
        return (f"<RecordingImage rec={self.recording_id} "
                f"{self.filename}{' PRIMARY' if self.is_primary else ''}>")


# Behaviour lives in app/utils/entity_images.py so every photographed entity
# stays in lockstep. Re-exported here to match the other image models'
# import shape.
from app.utils.entity_images import set_primary, primary_for   # noqa: E402,F401
