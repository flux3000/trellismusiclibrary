"""models/engineer.py -- Engineer (a person credited on a live recording's tape).

An Engineer is whoever taped, transferred or mastered a live recording
("Bornfree", "Charlie Miller", "MIKE"). Names are stored as the source wrote
them, nicknames included. Deliberately NOT linked to Musician: the same person
may exist in both tables and nothing joins them. Studio albums have no
engineer fields at all.

Recording carries three optional FKs (recording_engineer_id,
transfer_engineer_id, mastering_engineer_id), one engineer per role.
"""

from datetime import datetime, timezone
from app.extensions import db


class Engineer(db.Model):
    __tablename__ = "engineer"

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(255), nullable=False)
    sort_name  = db.Column(db.String(255), nullable=True)
    bio        = db.Column(db.Text,        nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    # Photos -- many, one flagged primary, primary first (same as Musician).
    images = db.relationship("EngineerImage", back_populates="engineer",
                             cascade="all, delete-orphan",
                             order_by="desc(EngineerImage.is_primary), "
                                      "EngineerImage.sort_order, EngineerImage.id")

    def __repr__(self):
        return f"<Engineer {self.name}>"


class EngineerImage(db.Model):
    """Photos of an Engineer. Same shape as MusicianImage; every behaviour lives
    in app/utils/entity_images.py keyed off `__parent_fk__`."""
    __tablename__ = "engineer_image"

    __parent_fk__ = "engineer_id"

    id          = db.Column(db.Integer, primary_key=True)
    engineer_id = db.Column(db.Integer,
                            db.ForeignKey("engineer.id", ondelete="CASCADE"),
                            nullable=False, index=True)

    # Basename only, inside the engineer's _images dir. Never a full path.
    filename   = db.Column(db.String(255), nullable=False)
    ext        = db.Column(db.String(8), nullable=False)

    is_primary = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    sort_order = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    origin     = db.Column(db.String(24), nullable=False, default="upload", server_default="upload")
    caption    = db.Column(db.String(255), nullable=True)
    credit     = db.Column(db.String(255), nullable=True)
    source_ref = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    engineer = db.relationship("Engineer", back_populates="images")

    def __repr__(self):
        return (f"<EngineerImage engineer={self.engineer_id} "
                f"{self.filename}{' PRIMARY' if self.is_primary else ''}>")
