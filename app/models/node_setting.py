"""app/models/node_setting.py -- per-install key/value settings.

Distinct from UserPreference (user_id-scoped, app/models/user_preference.py): a
row here is a fact about THIS INSTALL, not about any one person on it. See
app/utils/node_settings.py for the env-first, DB-fallback read/write.
"""

from app.extensions import db


class NodeSetting(db.Model):
    __tablename__ = "node_setting"

    key   = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(500), nullable=False)

    def __repr__(self):
        return f"<NodeSetting {self.key}={self.value!r}>"
