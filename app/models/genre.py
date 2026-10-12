"""models/genre.py — Genre model.

Genre is a proper dimension: its own table, referenced by a single FK from
Artist. Nothing may create a Genre implicitly — pickers only ever select from
this table, never write free text into it. Every Genre has a colour. That's the entire point of the
design: without the FK this becomes 164 spellings of "bluegrass".

One genre per artist (a strict FK, not M2M) — the user's explicit call. A
migration to a `artist_genre` join table is mechanical if that ever bites, but
not built now.
"""

from datetime import datetime, timezone
from app.extensions import db


class Genre(db.Model):
    __tablename__ = "genre"

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(80), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)

    # Display colour — a `#rrggbb` hex string. Always set: the picker's choice,
    # else utils.genre_color.default_genre_color() at creation, and an update
    # cannot clear it. The column stays nullable at the DB level only because
    # SQLite cannot drop NOT NULL without a table rebuild; ensure_genre_colors
    # fills any NULL at startup. An artist with no genre has no colour to
    # inherit and renders neutral grey in the frontend.
    color       = db.Column(db.String(7), nullable=True)
    created_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                            onupdate=lambda: datetime.now(timezone.utc))

    artists = db.relationship("Artist", back_populates="genre")

    def __repr__(self):
        return f"<Genre {self.name}>"
