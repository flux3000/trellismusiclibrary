"""
models/performance_personnel.py — show-level personnel (2026-07-18).

Layer 2 of the Per-Show Personnel model (see Context Library/Per-Show
Personnel - Design Plan). A PerformancePersonnel row says "this Musician
played this specific Performance," independent of the act's Membership
roster. What it means depends on the parent Performance's personnel_mode:

  'inherit'  — these rows are ADDITIONS to the resolved act roster (guests,
               sit-ins). is_guest=True is the expected case here.
  'explicit' — these rows ARE the entire lineup; the act roster is ignored.

Always read through app/utils/personnel.py::resolve_performance_personnel()
rather than querying this table directly — it's the one place inherit vs.
explicit resolution happens, so every endpoint/view agrees.
"""

from datetime import datetime, timezone
from app.extensions import db


class PerformancePersonnel(db.Model):
    __tablename__ = "performance_personnel"

    id             = db.Column(db.Integer, primary_key=True)
    performance_id = db.Column(db.Integer, db.ForeignKey("performance.id"), nullable=False)
    musician_id      = db.Column(db.Integer, db.ForeignKey("musician.id"),      nullable=False)

    instrument = db.Column(db.String(128), nullable=True)   # "banjo", "mandolin"
    order      = db.Column(db.Integer, nullable=False, default=0)
    is_guest   = db.Column(db.Boolean, nullable=False, default=False)
    note       = db.Column(db.String(255), nullable=True)   # "first set only", "encore only"

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    performance = db.relationship("Performance", back_populates="personnel")
    musician      = db.relationship("Musician")

    def __repr__(self):
        return f"<PerformancePersonnel performance={self.performance_id} musician={self.musician_id}>"
