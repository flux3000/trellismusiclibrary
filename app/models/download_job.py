"""
models/download_job.py -- the archive download queue (2026-10-01).

One row per "download this show from an archive" request. One queue per
install (no user_id): the Downloads folder is the install's, not a person's.
Paused/running is a node setting (app/utils/node_settings.py), not a column --
it is a fact about the queue, not about any one job.

Statuses: queued, active, done, failed, cancelled, cleared. `cleared` is a
finished job the user dismissed from the panel; the row stays so history is
not lost, it just stops being listed.
"""

from datetime import datetime, timezone
from app.extensions import db


def _now():
    return datetime.now(timezone.utc)


class DownloadJob(db.Model):
    __tablename__ = "download_job"

    id = db.Column(db.Integer, primary_key=True)

    # "lma" today, "bga" later.
    source = db.Column(db.String(16), nullable=False)
    # The archive's own identifier (an IA item id for lma).
    source_id = db.Column(db.String(255), nullable=False)

    # Denormalised so the queue panel renders without asking the archive again.
    # date is YYYY-MM-DD or a partial form of it.
    artist = db.Column(db.String(255), nullable=True)
    date = db.Column(db.String(32), nullable=True)
    venue = db.Column(db.String(255), nullable=True)

    status = db.Column(db.String(16), nullable=False, default="queued",
                       server_default="queued", index=True)
    # Order among queued jobs; lowest goes first.
    position = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    total_bytes = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    done_bytes = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")

    # Absolute path of the folder this job writes into.
    dest_path = db.Column(db.String(1024), nullable=False)
    # Short, shown in the panel.
    error = db.Column(db.Text, nullable=True)
    # JSON {"verified": n, "mismatched": [names]} written when a job finishes
    # `done` (2026-10-01). NULL for a job that never completed. Drives the
    # Downloads page fingerprint icon; a file the archive gave no MD5 for is in
    # neither list.
    checksums = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<DownloadJob {self.id} {self.source}:{self.source_id} {self.status}>"
