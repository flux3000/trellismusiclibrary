"""
models/performance.py — Performance model.

A Performance is a unique live event: a specific artist on a specific
night at a specific place. Multiple recordings (different tapes, sources)
can exist for the same performance.

Location resolution order (app layer):
  1. performance.venue_id       — known venue record
  2. performance.city/state     — known city but no venue record
  3. event.venue_id / city      — fall back to parent event location
  4. Unknown
"""

from datetime import datetime, timezone
from app.extensions import db


class Performance(db.Model):
    __tablename__ = "performance"

    id           = db.Column(db.Integer, primary_key=True)
    artist_id = db.Column(db.Integer, db.ForeignKey("artist.id"), nullable=False)
    venue_id     = db.Column(db.Integer, db.ForeignKey("venue.id"),  nullable=True)
    event_id  = db.Column(db.Integer, db.ForeignKey("event.id"),  nullable=True)

    # Optional title for named performances (e.g. "The Last Waltz")
    title  = db.Column(db.String(255), nullable=True)

    # Sub-venue stage (e.g. "Omega Tent", "Main Stage") — used within events
    stage  = db.Column(db.String(128), nullable=True)

    # 'inherit' (default): lineup = act Memberships whose stints cover this
    # date, plus any performance_personnel rows layered on as guests/edits.
    # 'explicit': lineup = performance_personnel rows ONLY; act roster ignored
    # entirely (rotating billings where the act roster is just a pick-list,
    # e.g. Acoustic All-Stars). Defaults from artist.default_personnel_mode
    # at creation. See app/utils/personnel.py::resolve_performance_personnel.
    personnel_mode = db.Column(db.String(16), nullable=False, default="inherit",
                               server_default="inherit")

    # Date range — nullable integers support partial and multi-day dates
    start_year  = db.Column(db.Integer, nullable=True)
    start_month = db.Column(db.Integer, nullable=True)
    start_day   = db.Column(db.Integer, nullable=True)
    end_year    = db.Column(db.Integer, nullable=True)   # null unless multi-day
    end_month   = db.Column(db.Integer, nullable=True)
    end_day     = db.Column(db.Integer, nullable=True)

    # Location fallback when venue_id is null but city is known
    city    = db.Column(db.String(128), nullable=True)
    state   = db.Column(db.String(64),  nullable=True)
    country = db.Column(db.String(64),  nullable=True)

    notes      = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    artist  = db.relationship("Artist",  back_populates="performances")
    venue      = db.relationship("Venue",      back_populates="performances")
    event      = db.relationship("Event",      back_populates="performances")
    recordings = db.relationship("Recording",  back_populates="performance",
                                 cascade="all, delete-orphan")
    personnel  = db.relationship("PerformancePersonnel", back_populates="performance",
                                 cascade="all, delete-orphan",
                                 order_by="PerformancePersonnel.order")

    def __repr__(self):
        date = f"{self.start_year}-{self.start_month:02d}-{self.start_day:02d}" \
               if all([self.start_year, self.start_month, self.start_day]) else "unknown date"
        return f"<Performance {self.artist_id} @ {date}>"
