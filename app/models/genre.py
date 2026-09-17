"""
models/genre.py — Genre model.

Genre is a proper dimension (Ryan, 2026-08-02 — see the Genre design spec in
Context Library): its own table, referenced by a single FK from Artist.
Nothing may create a Genre implicitly — pickers only ever select from this
table, never write free text into it. That's the entire point of the design:
without the FK this becomes 164 spellings of "bluegrass".

One genre per artist (a strict FK, not M2M) — Ryan's explicit call. A
migration to a `artist_genre` join table is mechanical if that ever bites,
but not built now.
"""

from datetime import datetime, timezone
from app.extensions import db


class Genre(db.Model):
    __tablename__ = "genre"

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(80), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)

    # Display colour (2026-08-07) — a `#rrggbb` hex string, set by the colour
    # picker in the Genre editor. Drives the Browse cards' colour flair.
    #
    # Nullable on purpose, and NULL is a first-class state, not a gap to fill:
    # 70 of 164 artists have no genre at all, so a card frequently has no
    # colour to inherit. Those render a neutral grey (Ryan, 2026-08-07) — quiet
    # rather than broken, consistent with every other Browse element degrading
    # silently on NULL. Resolution therefore always goes through the frontend's
    # fallback, never straight at this column.
    color       = db.Column(db.String(7), nullable=True)
    created_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                            onupdate=lambda: datetime.now(timezone.utc))

    artists = db.relationship("Artist", back_populates="genre")

    def __repr__(self):
        return f"<Genre {self.name}>"
