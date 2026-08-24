"""
models/remote_favorite.py — MY favourites, in someone else's library.

`Recording.is_favorite` is a column on a recording I own. It cannot answer this
question, because the recording being starred lives in a different database on
a different machine — I only ever see it through the proxy.

WHY THIS LIVES ON THE CONSUMER AND NOT THE HOST
-----------------------------------------------
The obvious alternative is a `peer_favorite` table on the sharer's node, keyed
by peer. It was rejected for two reasons:

1. **The peer door has no editing endpoints, structurally.** That is the whole
   premise of api/share.py — a peer token cannot mutate anything because there
   is nothing there to mutate, rather than because every handler remembers to
   check. Adding the first write endpoint to that blueprint trades a
   load-bearing security property for convenience.
2. **A listener's taste is their own business.** Storing Matt's stars in Ryan's
   database means Ryan can read what Matt likes. That is not information the
   act of sharing a library ought to hand over — the same argument that keeps
   `play_log` from travelling in the other direction.

WHAT IS STORED, AND WHAT IS NOT
-------------------------------
Only the PAIR (which library, which recording id). No performer, no date, no
venue — nothing about the recording itself.

That keeps faith with "live proxy, zero persistence": the rule forbids caching
the REMOTE'S data locally, because a stale copy silently disagrees with the
source. A favourite is not their data. It is my judgement ABOUT their
recording, and it is the only thing here that is genuinely mine.

The consequence, accepted deliberately: rendering these rows requires asking
the remote for them (one batched call — see `/api/share/recordings/by-ids`), so
a favourite whose library is offline shows as unavailable rather than as a
stale row. That is the honest failure.

⚠ `remote_recording_id` is NOT a foreign key. It is an id in a database this
process cannot see, and declaring an FK against a local `recording.id` would
silently join to whatever unrelated show happens to hold that number here.
"""

from datetime import datetime, timezone
from app.extensions import db


class RemoteFavorite(db.Model):
    """One starred recording in one joined remote library."""
    __tablename__ = "remote_favorite"

    id                  = db.Column(db.Integer, primary_key=True)
    remote_node_id      = db.Column(db.Integer, db.ForeignKey("remote_node.id"),
                                    nullable=False, index=True)
    # Deliberately not a ForeignKey — see the module docstring.
    remote_recording_id = db.Column(db.Integer, nullable=False, index=True)
    created_at          = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    node = db.relationship("RemoteNode")

    __table_args__ = (
        # Starring twice is the same star. Enforced in the schema rather than
        # in the handler because the handler is not the only possible writer.
        db.UniqueConstraint("remote_node_id", "remote_recording_id",
                            name="uq_remote_favorite_node_recording"),
    )

    def __repr__(self):
        return (f"<RemoteFavorite node={self.remote_node_id} "
                f"rec={self.remote_recording_id}>")
