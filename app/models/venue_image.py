"""
models/venue_image.py — multiple images per Venue, one designated primary.

A parallel table to `artist_image` rather than a shared polymorphic one
(Ryan, 2026-08-07). The duplication is SCHEMA ONLY: every behaviour —
one-primary maintenance, promotion on delete, upload/serve/delete — lives once
in `app/utils/entity_images.py` and is parameterised by `__parent_fk__`.

Why parallel rather than polymorphic: a real foreign key. FK enforcement was
turned on deliberately in July, and SQLite cannot enforce an (entity_type,
entity_id) pair at all — so a polymorphic table would trade a guarantee the
database gives us for the convenience of one fewer CREATE TABLE.

Files live at LIBRARY_ROOT/_venues/<sanitized venue name>/_images/<filename>.
The `_venues` prefix keeps them out of the artist namespace: a venue and an
act can share a name ("Fillmore"), and two entities writing to one folder is a
collision waiting to happen.
"""

from datetime import datetime, timezone
from app.extensions import db


class VenueImage(db.Model):
    __tablename__ = "venue_image"

    # The one thing that differs from ArtistImage, and the hook every shared
    # helper in utils/entity_images.py keys off.
    __parent_fk__ = "venue_id"

    id       = db.Column(db.Integer, primary_key=True)
    venue_id = db.Column(db.Integer,
                         db.ForeignKey("venue.id", ondelete="CASCADE"),
                         nullable=False, index=True)

    # Basename only, inside the venue's _images dir. Never a full path — the
    # library root is config, and paths are deliberately not exposed to the
    # frontend (see the file-obfuscation rule in CONTEXT.md).
    filename   = db.Column(db.String(255), nullable=False)
    ext        = db.Column(db.String(8), nullable=False)

    is_primary = db.Column(db.Boolean, nullable=False, default=False,
                           server_default="0")
    sort_order = db.Column(db.Integer, nullable=False, default=0,
                           server_default="0")

    origin     = db.Column(db.String(24), nullable=False, default="upload",
                           server_default="upload")
    caption    = db.Column(db.String(255), nullable=True)
    credit     = db.Column(db.String(255), nullable=True)
    # Upstream id for a fetched image. Unused today — no Commons path exists for
    # venues — but present so the column doesn't have to be added later if one
    # does, and so the two image tables stay shape-compatible.
    source_ref = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(timezone.utc))

    venue = db.relationship("Venue", back_populates="images")

    def __repr__(self):
        return (f"<VenueImage venue={self.venue_id} "
                f"{self.filename}{' PRIMARY' if self.is_primary else ''}>")
