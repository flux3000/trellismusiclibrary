"""models/quality.py — Listening Quality persistence.

TWO tables, deliberately, because the score has two lifecycles:

  QualityAnalysis   staging, keyed by SOURCE FOLDER PATH, pre-ingest
  RecordingQuality  permanent, keyed by RECORDING, post-ingest

Why not one table with a nullable recording_id (which looks tidier)? Because
after a **Move** ingest the source folder no longer exists — `move_to_library`
removes it, and the empty-parent cleanup may remove its parent too. A
folder_path-keyed row physically cannot serve as the permanent record: its key
ceases to exist at exactly the moment the permanent record is needed. Splitting
them also gives staging rows a prunable lifecycle while permanent rows live and
die with their Recording.

On ingest commit the staging row's features/scores are copied across (see
`app/utils/quality_store.py::promote_to_recording`).

NFC WARNING: `folder_path` is ALWAYS stored NFC-normalised.  macOS hands out
decomposed (NFD) filenames while the DB holds composed (NFC), so a raw compare
silently fails on any accented name — this cost a whole afternoon on the Guitar
Trio corpus. Go through the helpers in `utils/quality_store.py` rather than
writing this column directly.
"""

from app.extensions import db


class _ScoreColumnsMixin:
    """
    The score payload, shared by both tables.

    A declarative mixin rather than a helper function: SQLAlchemy copies Column
    objects declared on a mixin into each subclass, which is exactly what's
    needed here (a single Column instance cannot belong to two tables).  Writing
    these columns out twice would guarantee the two tables drift apart on the
    next schema change.
    """

    # ── Headline ─────────────────────────────────────────────────────────────
    listening_quality = db.Column(db.Float, nullable=True)   # 0–100 composite
    score_tone        = db.Column(db.Float, nullable=True)
    score_noise       = db.Column(db.Float, nullable=True)
    score_dynamics    = db.Column(db.Float, nullable=True)

    # ── Payloads (JSON text) ─────────────────────────────────────────────────
    # Full raw feature dict, kept in full deliberately: scoring is a pure
    # function over these, so a re-weight is a re-score pass with NO audio
    # decode.  Dropping "unused" features would forfeit exactly that.
    features_json         = db.Column(db.Text, nullable=True)
    technical_issues_json = db.Column(db.Text, nullable=True)   # [] when clean
    flags_json            = db.Column(db.Text, nullable=True)   # informational
    # Which tracks/offsets were sampled — drives click-to-jump playback.
    sampled_json          = db.Column(db.Text, nullable=True)

    technical_deduction = db.Column(db.Float, nullable=True, default=0.0)

    # ── Version stamps — two, gating two different kinds of recompute ────────
    # analysis_version moves → must re-decode audio
    # score_version moves    → re-score stored features, no decode
    analysis_version = db.Column(db.String(16), nullable=True)
    score_version    = db.Column(db.String(16), nullable=True)


class QualityAnalysis(_ScoreColumnsMixin, db.Model):
    """
    Pre-ingest staging row, one per candidate show folder.

    Survives app restart on purpose: analysis is cheap (~2 s each) but a bulk
    run over a large folder is not, and the triage decisions layered on top are
    human work that must not evaporate because the app restarted mid-review.
    """
    __tablename__ = "quality_analysis"

    id = db.Column(db.Integer, primary_key=True)

    # NFC-normalised absolute path to the show folder.  Unique: one analysis per
    # folder, re-analysis updates in place.
    folder_path = db.Column(db.String(1024), nullable=False, unique=True, index=True)
    # The parent directory the user actually scanned, so a run can be re-listed
    # without walking the filesystem again.
    source_dir  = db.Column(db.String(1024), nullable=True, index=True)
    # Display name (folder basename at analysis time).
    name        = db.Column(db.String(512), nullable=True)

    # ── Triage — the whole point of this table ───────────────────────────────
    # pending  → analysed, awaiting a human decision
    # accepted → proceeds to metadata review
    # rejected → not worth ingesting; remembered so a re-scan of the same
    #            directory doesn't offer it again
    triage_status = db.Column(db.String(16), nullable=False,
                              default="pending", server_default="pending",
                              index=True)

    # Set once the folder has actually been ingested, so a re-scan can show
    # "already done" rather than re-offering it.  Nullable: rejected folders
    # never ingest, and SET NULL keeps the triage decision if the recording is
    # later deleted.
    recording_id = db.Column(db.Integer,
                             db.ForeignKey("recording.id", ondelete="SET NULL"),
                             nullable=True, index=True)

    # Populated when extraction fails (unreadable folder, no audio, …) so the UI
    # can show WHY a card is empty instead of silently dropping the row.
    error = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True),
                           server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True),
                           server_default=db.func.now(),
                           onupdate=db.func.now(), nullable=False)

    def __repr__(self):
        return (f"<QualityAnalysis {self.name!r} "
                f"lq={self.listening_quality} {self.triage_status}>")


class RecordingQuality(_ScoreColumnsMixin, db.Model):
    """
    Permanent per-recording score.  One row per Recording, replaced on
    re-analysis.

    Deliberately NOT an extension of `track_analysis`: different cardinality
    (recording vs track), different lifecycle, different version stamps.
    """
    __tablename__ = "recording_quality"

    id = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer,
                             db.ForeignKey("recording.id", ondelete="CASCADE"),
                             nullable=False, unique=True, index=True)

    analyzed_at = db.Column(db.DateTime(timezone=True),
                            server_default=db.func.now(), nullable=False)

    # Backref is `quality_score`, NOT `quality` — `recording.quality` is already
    # taken by the manual A/B+ letter grade, and the collision is a useful one to
    # preserve rather than paper over.  The two are different measurements by
    # design: the letter grade rates listening quality AND performance quality
    # (a human judgement); this score only ever measures the audio.  They
    # complement each other and neither replaces the other.
    recording = db.relationship(
        "Recording", backref=db.backref("quality_score", uselist=False,
                                        cascade="all, delete-orphan"))

    def __repr__(self):
        return f"<RecordingQuality rec={self.recording_id} lq={self.listening_quality}>"
