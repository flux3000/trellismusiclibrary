"""
models/user_preference.py — Per-user key/value preference store.

Extensible without schema changes. New preferences are just new rows.

Known keys (MVP):
  ai_model : the Anthropic model used for AI Assist.
"""

from app.extensions import db


class UserPreference(db.Model):
    __tablename__ = "user_preference"

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    key     = db.Column(db.String(64),  nullable=False)
    value   = db.Column(db.String(255), nullable=False)

    # Relationship
    user = db.relationship("User", back_populates="preferences")

    def __repr__(self):
        return f"<UserPreference user={self.user_id} {self.key}={self.value}>"
