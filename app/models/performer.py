"""
models/performer.py — Performer (a performing act) and its member ordering.

A Performer is the billed act: the name that goes on the FLAC ARTIST tag and
that headlines a browse row — "Béla Fleck and Jerry Douglas", "Grateful Dead",
"Béla Fleck". Its members are Artists (people), linked via Membership.

A Performer must have at least one member. A solo act's single member is just
the person of the same name.

(2026-07-11 remodel: Performer = the act; Artist = a person; CanonicalArtist
is gone — aggregation of "everything by X" hangs off the person via Membership.)
"""

from datetime import datetime, timezone
from app.extensions import db


class Performer(db.Model):
    __tablename__ = "performer"

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(255), nullable=False)   # billed act name
    sort_name  = db.Column(db.String(255), nullable=True)
    bio        = db.Column(db.Text,        nullable=True)

    # Personnel resolution mode new Performances of this act start in.
    # 'inherit' (default) = act roster/stints apply; 'explicit' = every show
    # starts with an empty lineup that must be entered per-show (rotating
    # billings like "Acoustic All-Stars" set this once and never fight the
    # default again). See app/utils/personnel.py. No manual UI control since
    # the 2026-07-22 Members/Guests redesign — the field and its automatic
    # behavior (new-performance default, case-5 auto-flip) are unchanged.
    default_personnel_mode = db.Column(db.String(16), nullable=False, default="inherit")

    # ⚠ LEGACY as of 2026-08-07 — NOTHING READS THIS. Superseded by the
    # `performer_image` table (multiple images, one flagged primary); see
    # app/models/performer_image.py. Kept only because SQLite cannot drop a
    # column without a full table rebuild, and this project has already been
    # bitten once by rebuild-adjacent DDL (the 07-22 stale-FK episode). The
    # migration backfilled every populated value here into a PerformerImage
    # row and left the file on disk untouched.
    #
    # Original note, still true of the replacement: the folder path is
    # derived from the Performer's CURRENT name at request time, not stored —
    # renaming an act without a corresponding folder rename would orphan an
    # already-uploaded picture. Matches how recording folders already behave
    # on a Performer rename (nothing renames those either); flagged here
    # rather than solved, per Ryan's scope-discipline call on this feature.
    image_ext = db.Column(db.String(8), nullable=True)

    # Latest AI Dossier research pass (2026-07-22) — a drafted biography +
    # suggested external resource links, JSON-encoded like
    # Recording.ai_research_json. Nothing is auto-applied: the Dossier UI
    # only ever proposes; Ryan copies the bio into `bio` and picks which
    # resource links to add, by hand — same "AI suggests, human approves"
    # rule as the ingest-side AI Assist (see the 2026-07-20/21 AI Assist
    # Refinement spec in Context Library — auto-apply was removed there
    # after it silently overwrote a recording with a wrong date).
    dossier_json = db.Column(db.Text, nullable=True)

    # Latest AI lineup-research pass (2026-09-07) — the researched roster with
    # per-person stint dates, JSON-encoded, held for review.
    #
    # ITS OWN COLUMN, not folded into dossier_json: the two passes are separate
    # actions on separate buttons with separate output shapes, and sharing one
    # blob would mean each run silently destroyed the other's result.
    #
    # I argued against persisting this at all — a saved roster proposal sits
    # beside the authoritative Membership rows and can drift from them. Ryan
    # overruled it (2026-09-07) and was right: research the human paid tokens
    # for, that vanishes on navigating away, is not a duplicate-data problem,
    # it is a lost-work problem. The drift concern is real but belongs in the
    # RENDER — the UI checks each row against the live roster and marks the
    # ones already applied, rather than trusting a stored "applied" flag.
    #
    # Nothing here is authoritative. It is a proposal awaiting a human, exactly
    # like dossier_json, and applying a row writes real Membership rows.
    lineup_json = db.Column(db.Text, nullable=True)

    # Genre (2026-08-02) — a proper dimension, its own table, one FK per
    # Performer (never M2M — see the Genre design spec in Context Library).
    # Nullable: all existing performers start unassigned, and a non-null
    # constraint would be wrong anyway — plenty of acts resist a single
    # label. Nothing may create a Genre implicitly; pickers only ever select
    # from the existing table.
    genre_id = db.Column(db.Integer, db.ForeignKey("genre.id"), nullable=True)

    # ── MusicBrainz (2026-08-07) ────────────────────────────────────────────
    # Structured facts fetched once at Performer creation — see
    # app/utils/musicbrainz.py. Stored as REAL COLUMNS rather than a JSON blob
    # (Ryan's call) so they are queryable: "which acts formed in New Orleans"
    # is a legitimate question and a blob cannot answer it.
    #
    # MusicBrainz is a curated database, not a model, so these land directly
    # rather than going through AI Assist's approve-first rule. The risk here
    # is wrong-ENTITY, not wrong-fact, which is what mb_status guards.
    #
    # NOTHING HERE OVERWRITES A HUMAN FIELD — not name, bio, genre, or members.
    mbid = db.Column(db.String(36), nullable=True, index=True)

    # Five distinct states, and the differences matter to the UI:
    #   None         — never looked up (pre-existing rows, or offline at create)
    #   'matched'    — the confidence gate chose it, no human involved
    #   'linked'     — a human picked it from the candidate list
    #   'ambiguous'  — real candidates exist but none clearly won; needs a human
    #   'none'       — looked, found nothing
    # Only 'ambiguous' and None surface a "Look up" prompt.
    #
    # 'matched' vs 'linked' exists purely so the page can say "Matched
    # automatically" or "Linked by you" ACCURATELY. Claiming an automatic match
    # for work a person did is a small lie that makes every other automatic
    # claim in the app less believable (Ryan, 2026-08-07).
    mb_status         = db.Column(db.String(16), nullable=True)
    mb_type           = db.Column(db.String(32), nullable=True)   # Group / Person
    mb_area           = db.Column(db.String(120), nullable=True)  # origin
    # Partial ISO strings straight from MusicBrainz ("1965" or "1965-03-01").
    # Deliberately NOT split into y/m/d integers like Performance dates: these
    # are display facts we never sort or filter on, and splitting them would
    # invent precision the source doesn't guarantee.
    mb_begin          = db.Column(db.String(10), nullable=True)
    mb_end            = db.Column(db.String(10), nullable=True)
    mb_disambiguation = db.Column(db.String(255), nullable=True)
    mb_links_json     = db.Column(db.Text, nullable=True)
    # Aliases, community tags, related acts, gender. JSON rather than columns
    # BECAUSE THEY ARE LISTS — the real-columns rule above applies to queryable
    # scalars ("which acts formed in New Orleans"); a list in a column is the
    # thing that rule exists to prevent, not an exception to it.
    #
    # Aliases matter more here than they look: taper info files spell act names
    # every possible way, and this is ground truth for reconciling them.
    mb_extra_json     = db.Column(db.Text, nullable=True)
    mb_checked_at     = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    # Ordered members (people). Cascade so deleting a Performer clears its links.
    memberships  = db.relationship("Membership", back_populates="performer",
                                   cascade="all, delete-orphan",
                                   order_by="Membership.order")
    performances = db.relationship("Performance", back_populates="performer")
    # Profile images (2026-08-07) — multiple, one flagged primary. Ordered so
    # the primary is first and `images[0]` is always the card face; see
    # performer_image.primary_for() for the same rule server-side.
    images       = db.relationship("PerformerImage", back_populates="performer",
                                   cascade="all, delete-orphan",
                                   order_by="desc(PerformerImage.is_primary), "
                                            "PerformerImage.sort_order, "
                                            "PerformerImage.id")
    # External reference resources (databases, discographies) — see PerformerResource.
    resources    = db.relationship("PerformerResource", back_populates="performer",
                                   cascade="all, delete-orphan",
                                   order_by="PerformerResource.order")
    genre        = db.relationship("Genre", back_populates="performers")

    @property
    def artists(self):
        """
        Member Artists (people) in billing order — each person appears once,
        even if they have multiple Membership STINTS (e.g. Mickey Hart's two
        Dead tenures). `memberships` is already ordered by Membership.order,
        so taking the first occurrence per artist naturally applies the
        design doc's "dedupe by artist, earliest stint wins the order" rule.
        """
        seen = {}
        for m in self.memberships:
            seen.setdefault(m.artist_id, m.artist)
        return list(seen.values())

    def __repr__(self):
        return f"<Performer {self.name}>"


class PerformerResource(db.Model):
    """
    An external reference for a Performer — a discography, tape database, or
    fan-maintained source of truth (e.g. the PMDB for Pat Metheny). Lives at the
    act level, not the person level.
    """
    __tablename__ = "performer_resource"

    id           = db.Column(db.Integer, primary_key=True)
    performer_id = db.Column(db.Integer, db.ForeignKey("performer.id"), nullable=False)
    label        = db.Column(db.String(255),  nullable=True)   # display name; falls back to URL
    url          = db.Column(db.String(1024), nullable=False)
    order        = db.Column(db.Integer, nullable=False, default=0)
    created_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    performer = db.relationship("Performer", back_populates="resources")

    def __repr__(self):
        return f"<PerformerResource {self.url} performer={self.performer_id}>"
