"""
models/user.py — User and permission models.

Roles:
  admin     - full access, manages users and library config
  archivist - can edit metadata; scope controlled by all_artists flag
              and user_artist_permission rows
  listener  - read-only, playback only
"""

from datetime import datetime, timezone
from flask_login import UserMixin
from app.extensions import db


class User(UserMixin, db.Model):
    __tablename__ = "user"

    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(64),  unique=True, nullable=False)
    email         = db.Column(db.String(255), unique=True, nullable=True)

    # ── Identity, 2026-08-25 ──────────────────────────────────────────────────
    # `username` is the CREDENTIAL and stays put once chosen; `display_name` is
    # what a human sees, here and on any library this person shares. Two fields
    # because they answer different questions — Ryan's collecting partner signed
    # everything "oldindian" and was called Jeff, and renaming yourself should
    # not change how you log in.
    #
    # Nullable, falling back to `username`: every existing row predates this and
    # a blank name on screen is worse than a handle.
    display_name  = db.Column(db.String(120), nullable=True)

    # Extension only ('.jpg'). The file is Config.AVATAR_DIR/user_<id><ext> —
    # one picture per person, so this is a column rather than the row-per-image
    # table performers use. NB performer.image_ext is vestigial for exactly the
    # opposite reason: performers grew galleries. A face does not.
    avatar_ext    = db.Column(db.String(8), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)

    # "admin" | "archivist" | "listener"
    role          = db.Column(db.String(16),  nullable=False, default="listener")

    # When True (and role == "archivist"), permission table is ignored —
    # the user may edit records for any artist.
    all_artists   = db.Column(db.Boolean, nullable=False, default=False)

    is_active     = db.Column(db.Boolean, nullable=False, default=True)
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                              onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    artist_permissions = db.relationship("UserArtistPermission", back_populates="user",
                                         cascade="all, delete-orphan")
    preferences        = db.relationship("UserPreference",       back_populates="user",
                                         cascade="all, delete-orphan")
    play_logs          = db.relationship("PlayLog", back_populates="user")

    @property
    def name(self):
        """What to show. Never blank — falls back to the login handle."""
        return (self.display_name or "").strip() or self.username

    def __repr__(self):
        return f"<User {self.username} ({self.role})>"


class UserArtistPermission(db.Model):
    """
    Grants an archivist edit access to a specific Performer (act).
    Only evaluated when user.all_artists is False.
    """
    __tablename__ = "user_artist_permission"

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("user.id"),      nullable=False)
    performer_id = db.Column(db.Integer, db.ForeignKey("performer.id"), nullable=False)

    # Relationships
    user      = db.relationship("User", back_populates="artist_permissions")
    performer = db.relationship("Performer")

    def __repr__(self):
        return f"<UserArtistPermission user={self.user_id} performer={self.performer_id}>"
