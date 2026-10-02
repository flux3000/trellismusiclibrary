"""
models/recording.py — Recording and fingerprint models.

A Recording is one captured version of a Performance — a specific tape,
source, or transfer. Multiple recordings can exist for the same performance
(e.g. an SBD and an AUD of the same show).

File path obfuscation: folder_path is relative to LIBRARY_ROOT and is
never exposed to the frontend. The API serves audio by track ID only.
"""

from datetime import datetime, timezone
from app.extensions import db


class Recording(db.Model):
    __tablename__ = "recording"

    id             = db.Column(db.Integer, primary_key=True)
    performance_id = db.Column(db.Integer, db.ForeignKey("performance.id"), nullable=False)

    # Optional seeder-given name for this specific version
    title          = db.Column(db.String(255), nullable=True)

    # Recording method: SBD, AUD, MTX (matrix), FM, etc.
    source          = db.Column(db.String(64),  nullable=True)

    # Free-text technical/distinguishing detail: transfer chain (e.g. "Nakamichi
    # CM300 > Sony D5 DAT > CD > EAC > FLAC"), taper credit, mic position — anything
    # that identifies this specific tape or distinguishes it from another recording
    # of the same source type for the same show. As of 2026-07, this absorbed the
    # old standalone source_modifier field ("Source Detail") — distinguishing
    # between two same-source recordings is now done here or in notes, not a
    # separate structured field.
    lineage         = db.Column(db.Text,        nullable=True)

    # Letter grade: A+, A, A-, B+, B, B-, C — expanded in V2
    quality         = db.Column(db.String(4),   nullable=True)

    # False when recording is known to be missing tracks or cut
    is_complete    = db.Column(db.Boolean, nullable=False, default=True)

    # True when all or part of this recording has been officially released
    # (restricts streaming to other users in future multi-user builds)
    is_official    = db.Column(db.Boolean, nullable=False, default=False)

    # Path to recording folder, relative to LIBRARY_ROOT — never sent to frontend.
    # Indexed (N4): process() and _reconcile_review_items() both look a
    # recording up by folder_path per item, and that query was a full table
    # scan that grows with the library.
    folder_path    = db.Column(db.String(512), nullable=False, index=True)

    # In the library, or back out at the workbench? (2026-08-21)
    #
    # True  — the folder lives under LIBRARY_ROOT at folder_path and the show is
    #         part of the browsable collection. Every row ingested before this
    #         column existed backfills to True, which is correct: being in the
    #         database used to mean exactly this.
    # False — "Move to Workshop/Backlog" physically moved the folder OUT of the
    #         library. The audio still exists and the metadata, lineage,
    #         checksums and event history ingest produced are all still here;
    #         the show is simply off the shelf.
    #
    # Why a flag rather than deleting the row: deleting throws away everything
    # ingest learned, so a show that comes back has to be re-ingested from
    # scratch. Why not leave it published: folder_path would point at nothing,
    # and a recording that browses but cannot play is the failure mode CONTEXT.md
    # warns about — an empty state that is really a broken fetch.
    #
    # ⚠ folder_path is deliberately NOT rewritten on a move. It stays as the
    # library-relative path the show HAD, because it is only meaningful relative
    # to LIBRARY_ROOT and the folder is no longer under it. Where the folder
    # actually went is recorded in the RecordingEvent the move writes.
    is_published   = db.Column(db.Boolean, nullable=False, default=True,
                               server_default="1")

    # Original folder name as ingested (before any renaming)
    original_folder_name = db.Column(db.String(512), nullable=True)

    # Full text content of the accompanying info/text file
    info_file_content    = db.Column(db.Text, nullable=True)

    # RETIRED 2026-08-18 — "remove rating everywhere" (Ryan). Was a 0-100
    # holistic listener score. Nothing reads or writes it: it is gone from every
    # serializer, API payload, form and view. The column stays because 8 rows
    # carry a value and dropping it is the one step that cannot be undone; drop
    # it on Ryan's word. Do NOT reintroduce it into a payload — the product has
    # three quality signals already (the `quality` letter grade, the automated
    # listening_quality score, and is_favorite), and this was the fourth.
    rating     = db.Column(db.Integer, nullable=True)

    # Favorite / highlight star (2026-07-31). A purely human marker, and
    # deliberately the THIRD independent quality signal on a recording:
    #
    #   quality        letter grade — a considered judgement, needs thought
    #   quality_score  the automated Listening Quality analysis
    #   is_favorite    "this one is special", one click, no scale
    #
    # It exists because neither of the other two can express it. The letter
    # grade rates the artefact and the engine measures the audio; a highlight is
    # about the listener's relationship to the show and may well contradict both
    # — a rough audience tape of a once-in-a-lifetime night is a favorite and a
    # C. Kept boolean rather than a 1-5 star scale on purpose: this project
    # already has two graded scales that need deliberation, and the gap they
    # leave is a zero-effort one.
    is_favorite = db.Column(db.Boolean, nullable=False, default=False,
                            server_default="0")

    notes      = db.Column(db.Text, nullable=True)

    # etree-style naming facts (2026-09-25). Both are suggestions the user can
    # edit; never written by scan/detection without going through the form.
    # source_tag: gear/taper token (schoeps, nak300, miller...), the
    # {source_tag} naming token. etree_shnid: the LMA/etree source id
    # (118671), the {shnid} naming token.
    source_tag  = db.Column(db.String(64), nullable=True)
    etree_shnid = db.Column(db.Integer,    nullable=True)

    # Live show vs studio release (Bulk Ingest spec chunk 2). Studio
    # recordings may attach to a venue-less, possibly dateless Performance.
    kind = db.Column(db.String(16), nullable=False, default="live", server_default="live")

# MusicBrainz release lookup (Studio Records spec v1, section 2). Filled
    # fill-if-null only by try_match_release()/link_release() -- never
    # written for a live recording. mb_release_status mirrors the artist
    # mb_status values (None = never looked up) plus 'linked' (a human
    # picked it) and 'unlinked' (a human explicitly removed the link, so
    # the follow-up pass must never re-run it).
    mb_release_id         = db.Column(db.String(36),  nullable=True)
    mb_release_group_id   = db.Column(db.String(36),  nullable=True)
    mb_release_status     = db.Column(db.String(16),  nullable=True)
    mb_release_type       = db.Column(db.String(32),  nullable=True)
    mb_label              = db.Column(db.String(120), nullable=True)
    mb_catalog_number     = db.Column(db.String(64),  nullable=True)
    mb_release_country    = db.Column(db.String(2),   nullable=True)
    mb_release_checked_at = db.Column(db.DateTime,    nullable=True)

    # Artwork backfill follow-up (S8, independent review v1). NULL until the
    # "images" follow-up kind has scanned this recording's folder/embedded
    # art, whether or not it found anything -- same never-looked-up-yet
    # contract as mb_release_checked_at above. Any kind, not studio-only:
    # a live recording can carry taper art too.
    images_checked_at = db.Column(db.DateTime, nullable=True)

    # Latest AI Assist research result (JSON blob: thinking, proposals,
    # track_titles, verify_items, provenance_notes, sources). Overwritten on
    # each re-run — no history kept. Persisted so a research pass isn't lost
    # once the browser tab closes. NULL until AI Assist has been run.
    ai_research_json = db.Column(db.Text, nullable=True)

    # The ingest resolver's output as it stood at ingest time (JSON: per-field
    # value / source / candidates / conflict, plus kind, duplicates, status,
    # reasons; `tracks` deliberately omitted -- the recording's own tracks are
    # the truth). Provenance for the Resolver tab on View Recording. NULL for a
    # recording ingested before this column existed. Owner-only: never served
    # to peers (app/api/share.py does not read it).
    resolver_json = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    performance   = db.relationship("Performance",          back_populates="recordings")
    fingerprints  = db.relationship("RecordingFingerprint", back_populates="recording",
                                    cascade="all, delete-orphan")
    tracks        = db.relationship("Track",                back_populates="recording",
                                    cascade="all, delete-orphan",
                                    order_by="Track.track_number")
    events        = db.relationship("RecordingEvent",       back_populates="recording",
                                    order_by="RecordingEvent.created_at")
    # Studio Records spec v1 chunk 4 -- recording-level artwork, one flagged
    # primary. Sort mirrors artist/venue images (primary first, then
    # sort_order/id) via app/utils/entity_images.py's primary_for()/set_primary().
    images        = db.relationship("RecordingImage",       back_populates="recording",
                                    cascade="all, delete-orphan",
                                    order_by="RecordingImage.sort_order, RecordingImage.id")

    def __repr__(self):
        return f"<Recording {self.id} [{self.source}] performance={self.performance_id}>"


class RecordingFingerprint(db.Model):
    """
    Stores the content of integrity verification files (FFP, MD5)
    that accompany ROIO recordings. Used to verify file integrity over time.
    """
    __tablename__ = "recording_fingerprint"

    id               = db.Column(db.Integer, primary_key=True)
    recording_id     = db.Column(db.Integer, db.ForeignKey("recording.id"), nullable=False)

    # "ffp" (FLAC fingerprint) or "md5"
    fingerprint_type = db.Column(db.String(8), nullable=False)

    # Original filename as ingested
    filename         = db.Column(db.String(255), nullable=True)

    # Full file content
    content          = db.Column(db.Text, nullable=True)

    # This fingerprint file's location relative to the recording folder (e.g.
    # "CD1/checksum.md5"). Scopes checksum matching to one disc on a
    # multi-disc recording (2026-09-25). NULL until backfilled.
    rel_path         = db.Column(db.String(512), nullable=True)
    created_at       = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationship
    recording = db.relationship("Recording", back_populates="fingerprints")

    def __repr__(self):
        return f"<RecordingFingerprint {self.fingerprint_type} recording={self.recording_id}>"
