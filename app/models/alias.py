"""
models/alias.py -- learned aliases for venues and artists (Resolver v2, chunk 4).

The second layer the reader consults, between the user's own rows and the shipped
Atlas (design proposal 4.1): when a person confirms a venue or an artist that
differs from what the text said ("Wilkes Commnity College" saved as the Merlefest
venue), the text the evidence quoted is stored here, linked to the saved row. The
next import of a text that quotes it resolves at once.

These tables exist from chunk 4 and stay EMPTY until chunk 8 fills them. Rows are
written only from a human's confirmed save, only for venues and artists, and never
from an AI proposal the person did not accept.

`alias` is the text as quoted; `alias_key` is its normalised form (the same key the
library index and the Atlas use: accents, case, "&"/"and", punctuation and a
leading "The" ignored), which is what lookups compare. One row per (row, key).
Deleting a venue or artist deletes its aliases (ON DELETE CASCADE, and the ORM
cascade for sessions that do not rely on the pragma).
"""
from datetime import datetime, timezone

from app.extensions import db


class VenueAlias(db.Model):
    __tablename__ = "venue_alias"
    __table_args__ = (db.UniqueConstraint("venue_id", "alias_key", name="uq_venue_alias_key"),)

    id         = db.Column(db.Integer, primary_key=True)
    venue_id   = db.Column(db.Integer, db.ForeignKey("venue.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    alias      = db.Column(db.String(255), nullable=False)
    alias_key  = db.Column(db.String(255), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    venue = db.relationship("Venue", backref=db.backref(
        "aliases", cascade="all, delete-orphan", passive_deletes=True))


class ArtistAlias(db.Model):
    __tablename__ = "artist_alias"
    __table_args__ = (db.UniqueConstraint("artist_id", "alias_key", name="uq_artist_alias_key"),)

    id         = db.Column(db.Integer, primary_key=True)
    artist_id  = db.Column(db.Integer, db.ForeignKey("artist.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    alias      = db.Column(db.String(255), nullable=False)
    alias_key  = db.Column(db.String(255), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    artist = db.relationship("Artist", backref=db.backref(
        "aliases", cascade="all, delete-orphan", passive_deletes=True))
