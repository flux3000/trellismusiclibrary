"""models/musician.py — Musician (a person) and the Membership junction.

A Musician is an individual musician (Béla Fleck, Jerry Douglas, Sandip Burman).
Musicians are members of one or more Artists (acts) via Membership. Browsing
"everything by Béla Fleck" = every Artist he is a member of.
"""

from datetime import datetime, timezone
from app.extensions import db


class Musician(db.Model):
    __tablename__ = "musician"

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(255), nullable=False)   # person name
    sort_name  = db.Column(db.String(255), nullable=True)
    bio        = db.Column(db.Text,        nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    memberships = db.relationship("Membership", back_populates="musician",
                                  cascade="all, delete-orphan")
    # Photos — many, one flagged primary. Ordered primary-first so `images[0]`
    # is always the face this person shows, matching Artist and Venue.
    images      = db.relationship("MusicianImage", back_populates="musician",
                                  cascade="all, delete-orphan",
                                  order_by="desc(MusicianImage.is_primary), "
                                           "MusicianImage.sort_order, MusicianImage.id")

    def __repr__(self):
        return f"<Musician {self.name}>"


class Membership(db.Model):
    """Links a Musician (person) to an Artist (act). Ordered many-to-many.

    A row is a STINT, not a lifetime tie — multiple rows are allowed for the
    same (artist, musician) pair (e.g. Mickey Hart: Dead 1967-Feb1971, then
    Oct1974-1995). A person is in the lineup for a show if ANY of their stints
    covers that date (union of intervals).

    Bounds are nullable partial dates (y/m/d), same convention as Performance.
    Comparison normalizes coarse dates permissively:
      start -> earliest possible day (missing month/day -> 01/01)
      end   -> latest possible day   (missing month/day -> 12/last-day-of-month)
    See app/utils/personnel.py for the resolver that applies this rule.
    """
    __tablename__ = "membership"

    id           = db.Column(db.Integer, primary_key=True)
    artist_id = db.Column(db.Integer, db.ForeignKey("artist.id"), nullable=False)
    musician_id    = db.Column(db.Integer, db.ForeignKey("musician.id"),    nullable=False)
    order        = db.Column(db.Integer, nullable=False, default=0)

    # Instrument(s) played during THIS stint, comma-separated free text
    # ("fiddle, banjo"). Aggregate over the stint, not broken out
    # per-appearance, because that is the granularity the source data
    # itself has. See migrate_add_membership_instrument.py.
    instrument   = db.Column(db.String(128), nullable=True)

    # Stint bounds — nullable partial dates. NULL/NULL/NULL on both ends means
    # "always a member" (the default for every pre-existing row).
    start_year   = db.Column(db.Integer, nullable=True)
    start_month  = db.Column(db.Integer, nullable=True)
    start_day    = db.Column(db.Integer, nullable=True)
    end_year     = db.Column(db.Integer, nullable=True)
    end_month    = db.Column(db.Integer, nullable=True)
    end_day      = db.Column(db.Integer, nullable=True)

    artist = db.relationship("Artist", back_populates="memberships")
    musician    = db.relationship("Musician",    back_populates="memberships")

    def __repr__(self):
        return f"<Membership artist={self.artist_id} musician={self.musician_id}>"
