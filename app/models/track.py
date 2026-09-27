"""
models/track.py — Track model.

A Track is a single audio file within a Recording. Track titles default
to "Track {n}" when unknown so the UI always has something to display.

`set_number` is a free string (nullable) — examples: "Set 1", "Set 2",
"Early Set", "Late Set", "Encore", "Soundcheck". No enumeration enforced
because collector conventions vary widely. Named `set_number` rather than
`set` because `set` is a SQL reserved word — it bit a raw-SQL repair script
even though the ORM itself quotes it safely (2026-07-28).

`file_path` is relative to the recording's folder_path. Full resolution:
  LIBRARY_ROOT / recording.folder_path / track.file_path
"""

from datetime import datetime, timezone
from app.extensions import db


class Track(db.Model):
    __tablename__ = "track"

    id           = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer, db.ForeignKey("recording.id"), nullable=False)

    track_number = db.Column(db.Integer,     nullable=False)
    title        = db.Column(db.String(255), nullable=False)   # defaults to "Track {n}" on creation
    set_number   = db.Column(db.String(64),  nullable=True)    # e.g. "Set 1", "Encore" -- musical set only, never a disc (2026-09-25)

    # Disc, separate from set (2026-09-25). Populated only from disc carriers
    # (cd/disc/disk/d/vol/volume/part/tape/show subdirs, cd1t01/d1t01 filenames)
    # -- never from set carriers, and never bridged with set_number. NULL on a
    # single-disc recording. disc_track_number is this track's 1-based position
    # within its disc; track_number stays continuous across the whole recording.
    disc_number       = db.Column(db.Integer, nullable=True)
    disc_track_number = db.Column(db.Integer, nullable=True)

    # The rel_path this file had in the SOURCE folder at ingest time (e.g.
    # "CD1/01.flac"). Populated on every new ingest; used by the {original}
    # naming token and to scope checksum matching to one disc (2026-09-25).
    original_file_path = db.Column(db.String(512), nullable=True)

    # Duration in seconds, read from FLAC metadata on ingestion
    duration     = db.Column(db.Integer, nullable=True)

    # Filename relative to recording folder — never sent to frontend directly
    file_path    = db.Column(db.String(512), nullable=False)

    # True when this track has been officially released by the copyright holder
    is_official  = db.Column(db.Boolean, nullable=False, default=False)

    # JSON array of flag strings. Valid values (see TRACK_FLAGS in app.js —
    # that frontend registry is the single source of truth):
    #   start_truncated, end_truncated, incomplete, unknown_title, banter,
    #   tuning, audience, medley, announcement, interview, introduction,
    #   band_intros
    flags        = db.Column(db.Text, nullable=True)

    # Composer / songwriter credit (optional, not in standard FLAC tags)
    songwriter   = db.Column(db.String(255), nullable=True)

    # ── Checksum verification (2026-07-13) ────────────────────────────────────
    # Populated when a fingerprint file (.ffp / .md5 / .st5) accompanying the
    # source folder could be matched to this track. "ffp" and "st5" verify
    # against the FLAC file's own internal MD5 signature of the decoded audio
    # (tag/rename-proof); "md5" verifies the whole encoded file (breaks on any
    # tag edit, including Flux's own Write Tags to Files — expected, not a bug).
    # See app/utils/checksums.py for the read-the-authoritative-sources writeup.
    checksum_type         = db.Column(db.String(8),  nullable=True)   # ffp | md5 | st5
    expected_checksum     = db.Column(db.String(64), nullable=True)   # hex digest, as recorded in the fingerprint file
    checksum_status       = db.Column(db.String(16), nullable=True)   # match | mismatch | unverified
    checksum_verified_at  = db.Column(db.DateTime,   nullable=True)

    notes      = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    recording  = db.relationship("Recording", back_populates="tracks")
    play_logs  = db.relationship("PlayLog",   back_populates="track")

    def __repr__(self):
        return f"<Track {self.track_number}: {self.title} (recording={self.recording_id})>"
