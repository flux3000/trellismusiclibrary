"""app/utils/share_policy.py -- WHICH recordings may ever be shared with a peer.

The rule: only a LIVE recording that is not an official release may leave this
install. Everything else is invisible to every peer, whatever collections or
grants say.

A recording is shareable iff ALL of:
  1. kind == 'live'. A studio record never shares.
  2. is_official is False (the owner's own designation).
  3. It is not linked to a MusicBrainz release, OR the linked release's stored
     MusicBrainz status is 'Bootleg'.

Rule 3 is deliberately conservative. The MusicBrainz status is read from the
column stored at link time (recording.mb_release_db_status); MusicBrainz is
never called at share time. A linked release whose status is Official,
Promotion, Pseudo-Release, Withdrawn, Cancelled, or UNKNOWN (NULL: linked
before the status was stored, or the lookup omitted it) is treated as not
shareable. Unknown means no.

An already-shared record that becomes unshareable (flagged official, linked to
an Official release, kind changed) simply drops out of the peer's visible set
on the next request. Nothing is deleted, no grant or collection row changes,
and it returns the moment the designation is reversed.

Two independent forms of the same rule live here, one SQL and one Python, for
the same reason peer_access has two authorization paths: a test asserts they
agree, which only means something while neither calls the other.
"""

from sqlalchemy import and_, func, or_

SHAREABLE_MB_STATUSES = frozenset({"bootleg"})


def shareable_clause():
    """SQL predicate over Recording for the rule above."""
    from app.models.recording import Recording as R
    return and_(
        R.kind == "live",
        R.is_official.is_(False),
        or_(R.mb_release_id.is_(None),
            func.lower(R.mb_release_db_status).in_(SHAREABLE_MB_STATUSES)),
    )


def is_shareable(rec):
    """Python form of the rule, for one Recording row."""
    if rec is None or rec.kind != "live" or rec.is_official:
        return False
    if not rec.mb_release_id:
        return True
    return (rec.mb_release_db_status or "").lower() in SHAREABLE_MB_STATUSES


def shareable_recording_ids():
    """Ids of every shareable recording."""
    from app.extensions import db
    from app.models.recording import Recording
    rows = db.session.query(Recording.id).filter(shareable_clause()).all()
    return {rid for (rid,) in rows}
