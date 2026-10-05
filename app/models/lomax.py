"""
models/lomax.py -- Lomax runs and the proposals they make.

One lomax_run row is one research run and is also its job: a worker thread moves
status queued -> running -> done | error, and the status endpoint reads the row.
Proposals are rows so a human's accept or reject is recorded and a re-run can avoid
proposing a rejected value again.

For member and resource proposals (Artist History) `proposed` holds the entry as JSON.
"""
from datetime import datetime, timezone

from app.extensions import db


def _now():
    return datetime.now(timezone.utc)


class LomaxRun(db.Model):
    __tablename__ = "lomax_run"

    id           = db.Column(db.Integer, primary_key=True)
    skill        = db.Column(db.String(24),  nullable=False, index=True)
    subject_type = db.Column(db.String(16),  nullable=False)     # recording | folder | artist | venue
    subject_id   = db.Column(db.Integer,     nullable=True)
    subject_key  = db.Column(db.String(1024), nullable=True)     # folder path for "folder"
    level        = db.Column(db.String(12),  nullable=False, default="research")  # study | research (was read)
    question     = db.Column(db.Text,        nullable=True)
    input_json   = db.Column(db.Text,        nullable=True)      # what the page sent for a folder (unsaved import row)
    status       = db.Column(db.String(12),  nullable=False, default="queued")    # queued | running | done | error
    result_json  = db.Column(db.Text,        nullable=True)
    usage_json   = db.Column(db.Text,        nullable=True)
    model        = db.Column(db.String(64),  nullable=True)
    error        = db.Column(db.Text,        nullable=True)
    created_by   = db.Column(db.Integer,     nullable=True)
    created_at   = db.Column(db.DateTime,    default=_now)
    finished_at  = db.Column(db.DateTime,    nullable=True)
    # Which legacy column a run was copied from (ai_research_json, dossier_json, lineup_json);
    # null for a run Lomax made. Makes the one-time copy idempotent.
    migrated_from = db.Column(db.String(24), nullable=True)

    proposals = db.relationship("LomaxProposal", back_populates="run",
                                cascade="all, delete-orphan", order_by="LomaxProposal.id")

    __table_args__ = (db.Index("ix_lomax_run_subject", "subject_type", "subject_id"),
                      db.Index("ix_lomax_run_key", "subject_key"))


class LomaxProposal(db.Model):
    __tablename__ = "lomax_proposal"

    id         = db.Column(db.Integer, primary_key=True)
    run_id     = db.Column(db.Integer, db.ForeignKey("lomax_run.id"), nullable=False, index=True)
    field      = db.Column(db.String(32), nullable=False)
    current    = db.Column(db.Text, nullable=True)
    proposed   = db.Column(db.Text, nullable=False)
    confidence = db.Column(db.String(8), nullable=True)
    source     = db.Column(db.String(24), nullable=True)
    url        = db.Column(db.String(1024), nullable=True)
    decision   = db.Column(db.String(10), nullable=True)         # null | accepted | rejected
    # Resolution only: the field was one the resolver was confident about, so this proposal
    # challenges a settled value.
    challenge  = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    # Lomax agrees with the filed value: shown quietly, never applied, needs no decision.
    agrees     = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    decided_at = db.Column(db.DateTime, nullable=True)

    run = db.relationship("LomaxRun", back_populates="proposals")
