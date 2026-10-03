"""
models/bulk_ingest.py -- Bulk Ingest run/item tracking.

Durable, resumable bulk ingest of an existing library into Trellis
(spec chunk 2). A BulkIngestRun is one "point LIBRARY_ROOT at this folder and
ingest it" pass; a BulkIngestItem is one candidate folder found under that run's
root. Both survive an app restart on purpose -- a run over a real collector's
library is not a five-second operation, and progress must not evaporate
because the app quit mid-walk.

Counts (how many pending/ingested/failed/etc.) are deliberately NOT stored on
BulkIngestRun: they are derived by grouping BulkIngestItem.status at read time, the
same call this project already made for QualityAnalysis/triage -- a stored
counter and the rows it counts drift the moment one update forgets the other.
"""

from datetime import datetime, timezone
from app.extensions import db


class BulkIngestRun(db.Model):
    __tablename__ = "bulk_ingest_run"

    id = db.Column(db.Integer, primary_key=True)

    # Absolute path the run was pointed at. Not necessarily LIBRARY_ROOT --
    # "bring your own library" points this at wherever the existing collection
    # already lives.
    root = db.Column(db.String(1024), nullable=False)

    # running -> paused (user-initiated) -> running again, or running -> done.
    status = db.Column(db.String(16), nullable=False,
                       default="running", server_default="running", index=True)

    started_at = db.Column(db.DateTime(timezone=True),
                           default=lambda: datetime.now(timezone.utc),
                           nullable=False)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Set when the run stops on an unhandled error rather than finishing
    # normally, so the UI can say WHY it stopped instead of just "not done".
    last_error = db.Column(db.Text, nullable=True)

    # 'auto' (Import Automatically) ingests every clean folder as it is found;
    # 'hold' (Review First) analyzes each folder and parks it as 'ready' until
    # a person ingests it. Chosen at start (default: auto for a source inside
    # the library, hold for anywhere else) and never changed afterwards.
    mode = db.Column(db.String(8), nullable=False,
                     default="auto", server_default="auto")

    # JSON object of the "applies to every recording below" blanket values
    # (artist, venue, venue_id, city, state, country, event, source, lineage,
    # notes). Applied at ingest time in either mode, OVERWRITING what the scan
    # inferred for the fields it sets. NULL = nothing staged.
    applied_json = db.Column(db.Text, nullable=True)

    items = db.relationship("BulkIngestItem", back_populates="run",
                            cascade="all, delete-orphan")

    def __repr__(self):
        return f"<BulkIngestRun {self.id} {self.root!r} {self.status}>"


class BulkIngestItem(db.Model):
    __tablename__ = "bulk_ingest_item"

    id = db.Column(db.Integer, primary_key=True)

    run_id = db.Column(db.Integer,
                       db.ForeignKey("bulk_ingest_run.id", ondelete="CASCADE"),
                       nullable=False, index=True)

    # NFC-normalised path relative to the run's root, forward slashes always --
    # see the NFC warning in models/quality.py; the same macOS NFD trap applies
    # to any path compared against what the filesystem hands back.
    # N4: matches Recording.folder_path's own String(512) -- the two are
    # compared and joined against each other throughout bulk_ingest_run.py, so
    # a longer column here was never actually reachable (a Recording could
    # never carry a folder_path over 512 to begin with).
    rel_path = db.Column(db.String(512), nullable=False)

    # pending      -> not yet looked at
    # in_progress  -> currently being ingested
    # ingested      -> became a Recording
    # review       -> needs a human decision before it can proceed
    # skipped      -> deliberately not ingested (e.g. already in the library)
    # failed       -> bulk_ingest attempted and errored
    # ready        -> Review First: analyzed (and scored, if live), waiting for
    #                 a person to ingest it. Nothing is in the library yet.
    # moved        -> sent to Backlog/Workshop instead of ingested. Terminal;
    #                 not in the Queue and not in the library.
    status = db.Column(db.String(16), nullable=False,
                       default="pending", server_default="pending", index=True)

    # Short machine code for WHY status is review/skipped/failed (e.g.
    # "duplicate", "no_audio", "unreadable"). Free text detail goes in `detail`.
    reason = db.Column(db.String(32), nullable=True)
    detail = db.Column(db.Text, nullable=True)

    # 'live' / 'studio' -- set once classified; NULL until then.
    kind = db.Column(db.String(16), nullable=True)

    # Comma-joined set of audio formats found in the folder at extraction
    # time (2026-09-27 progress/log redesign) -- "FLAC", "MP3", "WAV", "SHN",
    # or a mix like "FLAC, MP3". NULL until classified, same as `kind`. Set
    # from every readable AUDIO_EXTENSIONS format actually found plus SHN
    # specifically (the one unsupported format worth naming in the log; see
    # extract() in app/utils/bulk_ingest.py).
    format = db.Column(db.String(32), nullable=True)

    # Small JSON blob of the fields extract() found for this folder (artist,
    # date_text, venue, city, state, country, source, title) -- 2026-09-27
    # progress/log redesign. Lets GET .../items show these without a
    # per-item re-scan; NULL for an item extract() never ran for (e.g. a
    # skip that short-circuited before extraction).
    meta = db.Column(db.Text, nullable=True)

    # The Recording this item became, once ingested. SET NULL (not CASCADE): the
    # BulkIngestItem is a durable record of the run and should outlive the
    # Recording it produced, e.g. if that Recording is later deleted.
    recording_id = db.Column(db.Integer,
                             db.ForeignKey("recording.id", ondelete="SET NULL"),
                             nullable=True, index=True)

    # Set when this item was found to duplicate an existing Recording, so the
    # review UI can point at what it duplicates.
    duplicate_of = db.Column(db.Integer,
                             db.ForeignKey("recording.id", ondelete="SET NULL"),
                             nullable=True)

    # A person asked for this ready/review item to be ingested. The worker (the
    # only thing that touches folders) picks requested items up and clears the
    # flag when it is done, whatever the outcome.
    ingest_requested = db.Column(db.Boolean, nullable=False,
                                 default=False, server_default="0")

    updated_at = db.Column(db.DateTime(timezone=True),
                           default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc),
                           nullable=False)

    __table_args__ = (
        db.UniqueConstraint("run_id", "rel_path", name="uq_bulk_ingest_item_run_rel_path"),
    )

    run = db.relationship("BulkIngestRun", back_populates="items")

    def __repr__(self):
        return f"<BulkIngestItem run={self.run_id} {self.rel_path!r} {self.status}>"
