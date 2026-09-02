"""
models/artist_image.py — multiple images per Artist (person), one primary.

Third parallel table alongside `performer_image` and `venue_image` (Ryan,
2026-09-01). It reverses the 2026-08-07 call that photos are performer-level
only — that decision was about the CARD surfaces, which still key off the act,
and it stands there. What changed is that the Artist page is now a real
dimension page with a Photos tab like every other, and a person is the one
entity in this schema that most obviously HAS a likeness.

Why parallel rather than polymorphic, again: a real foreign key. FK enforcement
was turned on deliberately in July and SQLite cannot enforce an
(entity_type, entity_id) pair at all, so a shared table would trade a guarantee
the database gives us for one fewer CREATE TABLE. The duplication is SCHEMA
ONLY — every behaviour lives once in `app/utils/entity_images.py`, keyed off
`__parent_fk__`.

Files live at LIBRARY_ROOT/_artists/<sanitized person name>/_images/<filename>.
The `_artists` prefix keeps them out of the performer namespace: a person and
an act routinely share a name (Bill Evans, Doc Watson), and two entities
writing to one folder is a collision waiting to happen.
"""

from datetime import datetime, timezone
from app.extensions import db


class ArtistImage(db.Model):
    __tablename__ = "artist_image"

    # The one thing that differs between the image tables, and the hook every
    # shared helper in utils/entity_images.py keys off.
    __parent_fk__ = "artist_id"

    id        = db.Column(db.Integer, primary_key=True)
    artist_id = db.Column(db.Integer,
                          db.ForeignKey("artist.id", ondelete="CASCADE"),
                          nullable=False, index=True)

    # Basename only, inside the person's _images dir. Never a full path — the
    # library root is config and paths are deliberately not exposed to the
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
    # Upstream id for a fetched image. No automatic Commons path exists for a
    # person today (the Wikidata bridge runs through the act's MusicBrainz
    # match), but the column is present so the two image tables stay
    # shape-compatible and one can be added without DDL.
    source_ref = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(timezone.utc))

    artist = db.relationship("Artist", back_populates="images")

    def __repr__(self):
        return (f"<ArtistImage artist={self.artist_id} "
                f"{self.filename}{' PRIMARY' if self.is_primary else ''}>")
