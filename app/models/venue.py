"""
models/venue.py — Venue model.

A Venue is a physical location — the canonical record for a place where
performances happened.
"""

from datetime import datetime, timezone
from app.extensions import db


class Venue(db.Model):
    __tablename__ = "venue"

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(255), nullable=False)  # canonical name
    city       = db.Column(db.String(128), nullable=True)
    state      = db.Column(db.String(64),  nullable=True)
    country    = db.Column(db.String(64),  nullable=True)
    bio        = db.Column(db.Text,        nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    performances = db.relationship("Performance",  back_populates="venue")
    events       = db.relationship("Event",        back_populates="venue")
    # Photos (2026-08-07) — many, one flagged primary. Ordered primary-first so
    # `images[0]` is always the card face, matching Artist.images.
    images       = db.relationship("VenueImage", back_populates="venue",
                                   cascade="all, delete-orphan",
                                   order_by="desc(VenueImage.is_primary), "
                                            "VenueImage.sort_order, VenueImage.id")

    def __repr__(self):
        return f"<Venue {self.name}, {self.city}>"
