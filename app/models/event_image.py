"""
models/event_image.py — multiple images per Event, one designated primary.

Fourth parallel table, added with the Event CRUD build (Ryan, 2026-09-01).
Same reasoning as venue_image and artist_image: parallel tables so each keeps a
real foreign key, shared behaviour in `app/utils/entity_images.py` keyed off
`__parent_fk__`. See app/models/venue_image.py for the full argument.

Files live at LIBRARY_ROOT/_events/<sanitized event name>/_images/<filename>.
An event ("Bonnaroo 2009") and an act can share a name, hence the prefix.
"""

from datetime import datetime, timezone
from app.extensions import db


class EventImage(db.Model):
    __tablename__ = "event_image"

    __parent_fk__ = "event_id"

    id       = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer,
                         db.ForeignKey("event.id", ondelete="CASCADE"),
                         nullable=False, index=True)

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
    source_ref = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(timezone.utc))

    event = db.relationship("Event", back_populates="images")

    def __repr__(self):
        return (f"<EventImage event={self.event_id} "
                f"{self.filename}{' PRIMARY' if self.is_primary else ''}>")
