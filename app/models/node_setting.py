"""
app/models/node_setting.py -- per-install key/value settings.

Distinct from UserPreference (user_id-scoped, app/models/user_preference.py):
a row here is a fact about THIS INSTALL, not about any one person on it. The
public share address is the first (and so far only) key -- decided
2026-08-27 when SHARE_BASE_URL turned out to be env-var-only, but the
DESKTOP APP (which actually mints invites) gets no env vars from the Dock.
See app/utils/node_settings.py for the env-first, DB-fallback read/write.
"""

from app.extensions import db


class NodeSetting(db.Model):
    __tablename__ = "node_setting"

    key   = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(500), nullable=False)

    def __repr__(self):
        return f"<NodeSetting {self.key}={self.value!r}>"
