"""
models/collection.py — Collection and its Recording junction.

A Collection is an optional, user-defined grouping of Recordings (a curated
set, a box, a project). Many-to-many: a Recording can be in many Collections,
a Collection holds many Recordings.

SYSTEM COLLECTIONS (2026-08-24)
-------------------------------
Peer sharing has exactly one primitive: the Collection grant. Whole-library
sharing therefore needs a Collection that MEANS "everything" rather than a
second sharing mechanism with its own authorization path — a second path is
how a leak gets written.

So a system collection is a real Collection row whose membership is resolved by
QUERY instead of by `collection_recording` junction rows. `system_key` is NULL
for every ordinary collection and non-NULL for the handful that are dynamic.
One nullable column rather than a boolean + a key, because `is_system=True` with
no key is a state that should not be representable.

⚠ THE TRAP: three separate places used to answer "what is in this collection?"
by querying the junction table directly (this model's `recordings`, and both
`recording_collection_ids` and `peer_visible_recording_ids` in
utils/peer_access.py). A dynamic collection has ZERO junction rows, so any path
that still reads the junction reports it as EMPTY — and an empty library is
indistinguishable from a broken share, which is this project's most-repeated
failure mode. Membership resolution lives HERE, on the model, so every caller
inherits it. Do not re-derive it at a call site.
"""

from datetime import datetime, timezone
from app.extensions import db

# The only system collection today. Deliberately a single hardcoded case rather
# than a rule/criteria language (Ryan, 2026-08-24): generalise only if a second
# real case turns up, not in anticipation of one.
SYSTEM_FULL_LIBRARY = "full_library"


class Collection(db.Model):
    __tablename__ = "collection"

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text,        nullable=True)
    created_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                            onupdate=lambda: datetime.now(timezone.utc))

    # NULL for an ordinary (junction-backed) collection; a stable key such as
    # "full_library" for a dynamic one. Unique so a node cannot end up with two
    # Full Library rows. Note ids differ per node — look a system collection up
    # by KEY, never by a hardcoded id.
    system_key  = db.Column(db.String(50), nullable=True, unique=True, index=True)

    recording_links = db.relationship("CollectionRecording", back_populates="collection",
                                      cascade="all, delete-orphan",
                                      order_by="CollectionRecording.order")

    @property
    def is_system(self):
        """A dynamic collection whose membership is a query, not junction rows."""
        return self.system_key is not None

    def _system_query(self):
        """The queryset behind a system collection. One branch per system_key.

        Full Library is every PUBLISHED recording. `is_published` is False for a
        show moved out to Workshop or Backlog, and such a show must stop being
        shared — its folder is no longer under LIBRARY_ROOT, so it would browse
        but not play, which is exactly the "empty state that is really a broken
        fetch" CONTEXT.md warns about.

        ⚠ This is the ONLY thing in the codebase that filters on `is_published`
        (reversing the 2026-08-21 "nothing filters on it" call, for this case
        only — Ryan, 2026-08-24). It is invisible in testing while every
        recording is published, so it is covered by a test that creates an
        unpublished row rather than by inspection.
        """
        from app.models.recording import Recording
        if self.system_key == SYSTEM_FULL_LIBRARY:
            return Recording.query.filter(Recording.is_published.is_(True))
        # An unknown key must not silently mean "everything". Fail closed.
        raise ValueError(f"Unknown system collection key: {self.system_key!r}")

    def resolved_recording_ids(self):
        """THE membership answer, as a set of ids. Every authorization path and
        every count derives from this — see the trap in the module docstring."""
        from app.models.recording import Recording
        if self.is_system:
            rows = self._system_query().with_entities(Recording.id).all()
        else:
            rows = (db.session.query(CollectionRecording.recording_id)
                    .filter_by(collection_id=self.id).all())
        return {rid for (rid,) in rows}

    @property
    def recording_count(self):
        """Count without materialising the rows. `len(c.recordings)` would build
        580 ORM objects to render one number in a sidebar."""
        if self.is_system:
            return self._system_query().count()
        return (db.session.query(CollectionRecording)
                .filter_by(collection_id=self.id).count())

    @property
    def recordings(self):
        """Ordered membership. Junction order for ordinary collections; newest
        first for Full Library, which has no curated order to honour."""
        from app.models.recording import Recording
        if self.is_system:
            return self._system_query().order_by(Recording.created_at.desc()).all()
        return [l.recording for l in self.recording_links]

    def __repr__(self):
        return f"<Collection {self.name}>"


class CollectionRecording(db.Model):
    """Junction linking a Recording to a Collection (ordered)."""
    __tablename__ = "collection_recording"

    id            = db.Column(db.Integer, primary_key=True)
    collection_id = db.Column(db.Integer, db.ForeignKey("collection.id"), nullable=False)
    recording_id  = db.Column(db.Integer, db.ForeignKey("recording.id"),  nullable=False)
    order         = db.Column(db.Integer, nullable=False, default=0)
    added_at      = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    collection = db.relationship("Collection", back_populates="recording_links")
    recording  = db.relationship("Recording")

    def __repr__(self):
        return f"<CollectionRecording collection={self.collection_id} recording={self.recording_id}>"
