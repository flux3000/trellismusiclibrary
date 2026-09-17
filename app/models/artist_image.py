"""
models/artist_image.py — multiple images per Artist, one designated primary.

Replaces the single `Artist.image_ext` slot (2026-08-07). That column is left
in place rather than dropped: SQLite cannot drop a column without a full table
rebuild, and this project has already been burned once by rebuild-adjacent DDL
(the 07-22 stale-FK episode). It is now LEGACY — nothing reads it. The migration
backfills every populated `image_ext` into a row here as the primary image, and
deliberately does not move the file on disk, so a half-run migration can never
orphan a photo.

Files live beside the old one, at
    LIBRARY_ROOT/<sanitized artist name>/_images/<filename>
with `filename` stored explicitly rather than derived from the row id. Two
reasons: the id isn't known until after flush (so a derived name needs a second
write), and an explicit column lets the backfilled legacy file keep its original
`profile.jpg` name without special-casing it forever.

PRIMARY IS ENFORCED IN APP LOGIC, not a DB constraint. SQLite can't express
"at most one row per artist with is_primary = 1" as a partial unique index
portably through SQLAlchemy's create_all, so `set_primary()` clears siblings in
the same transaction and is the ONLY sanctioned way to set the flag.
"""

from datetime import datetime, timezone
from app.extensions import db


class ArtistImage(db.Model):
    __tablename__ = "artist_image"

    # The hook every helper in app/utils/entity_images.py keys off — the only
    # thing that differs between this table and venue_image.
    __parent_fk__ = "artist_id"

    id           = db.Column(db.Integer, primary_key=True)
    artist_id = db.Column(db.Integer,
                             db.ForeignKey("artist.id", ondelete="CASCADE"),
                             nullable=False, index=True)

    # Basename only, inside the artist's _images dir. Never a full path —
    # the library root is config, and recording paths are deliberately never
    # exposed to the frontend (see the file-obfuscation rule in CONTEXT.md).
    filename   = db.Column(db.String(255), nullable=False)
    ext        = db.Column(db.String(8), nullable=False)

    is_primary = db.Column(db.Boolean, nullable=False, default=False,
                           server_default="0")
    sort_order = db.Column(db.Integer, nullable=False, default=0,
                           server_default="0")

    # Where it came from — 'upload' today, 'commons'/'ai' once the fetch job
    # lands. Kept from the start so auto-fetched images are distinguishable
    # from ones a human chose, which is what makes "candidates, not commits"
    # enforceable later rather than a retrofit.
    origin     = db.Column(db.String(24), nullable=False, default="upload",
                           server_default="upload")
    caption    = db.Column(db.String(255), nullable=True)
    credit     = db.Column(db.String(255), nullable=True)

    # Upstream identifier for a fetched image — the Commons filename. Its whole
    # job is DEDUPLICATION: clicking "Find a free photo" a second time must
    # return a different photo, and comparing bytes or credit strings would be
    # a guess where the source filename is exact. NULL for uploads.
    source_ref = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(timezone.utc))

    artist = db.relationship("Artist", back_populates="images")

    def __repr__(self):
        return (f"<ArtistImage perf={self.artist_id} "
                f"{self.filename}{' PRIMARY' if self.is_primary else ''}>")


# Behaviour lives in app/utils/entity_images.py so Artist and Venue photo
# management cannot drift apart (2026-08-07). Re-exported here because existing
# callers import these names from this module.
from app.utils.entity_images import set_primary, primary_for   # noqa: E402,F401
