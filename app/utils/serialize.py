"""
utils/serialize.py — Shared model → dict serialisers for API responses.

Single source of truth for the recording "summary" shape used by the catalog
(musicians), performance detail, and library views, so the fields never diverge
between endpoints.
"""

import json


def recording_summary(rec):
    """
    Compact recording dict for list/catalog contexts (not the full detail view).
    """
    return {
        "id":              rec.id,
        "source":          rec.source,
        # The MANUAL A/B+ letter grade. Rates the show as a whole — listening
        # quality AND performance quality — and is a human judgement.
        "quality":         rec.quality,
        # The AUTOMATED 0-100 Listening Quality score, measuring the audio only.
        # Deliberately a separate field: it complements the letter grade and
        # replaces neither. None until the recording has been analysed.
        "listening_quality": (rec.quality_score.listening_quality
                              if rec.quality_score else None),
        # `rating` (0-100 manual) was REMOVED from every payload 2026-08-18 at
        # Ryan's direction. Three quality signals was one too many: the letter
        # grade is the human judgement, listening_quality is the machine's, and
        # is_favorite is the one-click reaction. The DB column survives unread
        # (8 rows had a value) so the decision stays reversible.
        # One-click human highlight. Independent of both fields above — see the
        # comment on Recording.is_favorite.
        "is_favorite":     bool(rec.is_favorite),
        "is_complete":     rec.is_complete,
        "is_official":     rec.is_official,
        "track_count":     len(rec.tracks),
        # Total runtime in seconds (None-safe); powers the catalog length column.
        "duration_sec":    sum(t.duration or 0 for t in rec.tracks) or None,
        # When this recording was ingested (distinct from the show date) — powers
        # the sortable "Date Added" catalog column.
        "created_at":      rec.created_at.isoformat() if rec.created_at else None,
    }


def recording_row(rec, waveform=False, card=False):
    """
    Self-contained recording row for flat catalog/collection displays — includes
    the artist, date, and venue so a single row fully describes the show.

    `waveform` (default False, opt-in) adds a downsampled card waveform strip.
    Left off by default: this same serializer backs the flat List views
    (recent, collection, venue) — decoding TrackAnalysis JSON for every one of
    those rows would tax List to benefit the Browse card modules. Currently
    requested by nothing (the Browse card became a handbill on 2026-08-07);
    kept because the capability is real and tested.

    `card` (default False, opt-in) adds the Browse cards' visual fields:
    `genre`, `genre_color`, and `image_id` for the artist's primary photo.
    Same reasoning as `waveform` and the same design-spec rule: each field walks
    Recording → Performance → Artist → (Genre | ArtistImage), so on the
    544-row flat List that is three extra joins per row to benefit a 3-card and
    a 12-card module. Callers passing card=True MUST eager-load those
    relationships or they buy an N+1 — see api/recordings.py.
    """
    from app.utils.format import format_partial_date
    p = rec.performance
    v = p.venue if p else None
    row = {
        "id":              rec.id,
        "artist":       p.artist.name if (p and p.artist) else None,
        "artist_id":    p.artist_id if p else None,
        "date":            format_partial_date(p.start_year, p.start_month, p.start_day) if p else None,
        "start_year":      p.start_year  if p else None,
        "start_month":     p.start_month if p else None,
        "start_day":       p.start_day   if p else None,
        "venue":           v.name    if v else None,
        "city":            v.city    if v else (p.city    if p else None),
        "state":           v.state   if v else (p.state   if p else None),
        "country":         v.country if v else (p.country if p else None),
        "source":          rec.source,
        # The MANUAL A/B+ letter grade. Rates the show as a whole — listening
        # quality AND performance quality — and is a human judgement.
        "quality":         rec.quality,
        # The AUTOMATED 0-100 Listening Quality score, measuring the audio only.
        # Deliberately a separate field: it complements the letter grade and
        # replaces neither. None until the recording has been analysed.
        "listening_quality": (rec.quality_score.listening_quality
                              if rec.quality_score else None),
        # `rating` (0-100 manual) was REMOVED from every payload 2026-08-18 at
        # Ryan's direction. Three quality signals was one too many: the letter
        # grade is the human judgement, listening_quality is the machine's, and
        # is_favorite is the one-click reaction. The DB column survives unread
        # (8 rows had a value) so the decision stays reversible.
        # One-click human highlight. Independent of both fields above — see the
        # comment on Recording.is_favorite.
        "is_favorite":     bool(rec.is_favorite),
        "is_complete":     rec.is_complete,
        "track_count":     len(rec.tracks),
        "duration_sec":    sum(t.duration or 0 for t in rec.tracks) or None,
        # When this recording was ingested (distinct from the show date) — powers
        # the sortable "Date Added" catalog column.
        "created_at":      rec.created_at.isoformat() if rec.created_at else None,
    }
    if waveform:
        row["waveform"] = _card_waveform(rec)
    if card:
        artist = p.artist if (p and p.artist) else None
        g = artist.genre if artist else None
        row["genre"]       = g.name  if g else None
        # May be None even when a genre exists — colour is nullable and NULL is
        # a supported state (the frontend renders neutral grey). Never
        # substitute a default here; the fallback belongs in one place.
        row["genre_color"] = g.color if g else None
        row["image_id"]    = _primary_image_id(artist)
    return row


def _primary_image_id(artist):
    """
    Id of the artist's primary image, for the card's circular thumbnail.

    Reads through the `images` relationship (ordered primary-first) rather than
    querying, so an eager-loading caller pays nothing extra. Falls back to the
    first image when none is flagged primary — deleting the primary must not
    leave an artist with photos but no face on the card. Mirrors
    artist_image.primary_for(); both exist because one serves loaded objects
    and the other a bare id.
    """
    if artist is None:
        return None
    imgs = artist.images
    return imgs[0].id if imgs else None


def _card_waveform(rec, n=100):
    """
    Downsampled peaks for a recording's card waveform strip, sourced from its
    longest ANALYSED track — not necessarily track 1, which is often a short
    intro or tuning segment and makes a poor face for the show. None when no
    track on the recording has usable waveform_json (covers the small share of
    recordings with no analysis at all, and analysis rows saved without peaks).
    """
    from app.utils.waveform import downsample_peaks
    analysed = [t for t in rec.tracks if t.analysis and t.analysis.waveform_json]
    if not analysed:
        return None
    longest = max(analysed, key=lambda t: t.duration or 0)
    try:
        peaks = json.loads(longest.analysis.waveform_json)
    except (TypeError, ValueError):
        return None
    if not peaks:
        return None
    return downsample_peaks(peaks, n)
