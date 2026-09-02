"""
models/event.py — Event model.

An Event is a named container for one or more performances.
Examples: "Bonnaroo 2009" (festival), "Fall 1989 Tour" (tour run).

Performances within an event inherit the event's location by default,
but can override it via their own venue_id or city/state/country fields.
The `stage` field on Performance handles sub-venue distinctions (e.g. "Main Stage").
"""

from datetime import datetime, timezone
from app.extensions import db


class Event(db.Model):
    __tablename__ = "event"

    id      = db.Column(db.Integer, primary_key=True)
    name    = db.Column(db.String(255), nullable=False)  # e.g. "Bonnaroo 2009"

    # Optional anchor venue for the event (e.g. festival grounds)
    venue_id = db.Column(db.Integer, db.ForeignKey("venue.id"), nullable=True)

    # Fallback location when no venue is set
    city    = db.Column(db.String(128), nullable=True)
    state   = db.Column(db.String(64),  nullable=True)
    country = db.Column(db.String(64),  nullable=True)

    # Date range — use nullable integers to support partial dates
    start_year  = db.Column(db.Integer, nullable=True)
    start_month = db.Column(db.Integer, nullable=True)
    start_day   = db.Column(db.Integer, nullable=True)
    end_year    = db.Column(db.Integer, nullable=True)
    end_month   = db.Column(db.Integer, nullable=True)
    end_day     = db.Column(db.Integer, nullable=True)

    notes      = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    venue        = db.relationship("Venue",       back_populates="events")
    performances = db.relationship("Performance", back_populates="event")
    # Photos (2026-09-01) — many, one flagged primary, ordered primary-first.
    images       = db.relationship("EventImage", back_populates="event",
                                   cascade="all, delete-orphan",
                                   order_by="desc(EventImage.is_primary), "
                                            "EventImage.sort_order, EventImage.id")

    def __repr__(self):
        return f"<Event {self.name}>"
