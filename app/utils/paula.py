"""
app/utils/paula.py — "Paula": the free, non-AI completeness/confidence scorer.

Named in the 2026-07-16 design conversation with Ryan as shorthand for the
deterministic (regex/fuzzy-match) info parser, paired with "Jeff" for the
paid AI Assist research pass (app/utils/ai_assist.py). Paula runs
automatically, for free, as part of every folder scan — Jeff is an explicit,
opt-in, paid action layered on top when a human wants deeper research.

Paula answers two separate questions with two separate 0-100 scores:

  1. Primary Attribute Score — how confident are we in Artist, Date,
     Venue Name, City, State, and Country, based only on what's directly in
     the FLAC tags and the primary info text file (no web research)?
  2. Track Completeness Score — how much of the setlist do we already have,
     and does the FLAC TITLE tag agree with the info file's track listing?

Design history (2026-07-16 conversation):
  - Each Primary Attribute is scored from four raw signals: tag present,
    tag DB-matched, txt present, txt DB-matched. Weights reflect how much
    each attribute matters (Artist/Date highest; Country lowest, since
    domestic US shows almost never state it explicitly — see WEIGHTS).
  - IMPORTANT CALIBRATION FIX: a DB match is a catalog-consistency signal
    ("have we seen this exact name before"), not a truth signal. A brand
    new artist/venue that isn't in the DB yet is NOT less reliable data
    just because it's new — Ryan flagged this explicitly after an early
    version of this scorer badly under-scored a well-tagged show with a
    new-to-the-DB artist (58 instead of ~75). Match is now a modest
    +0.20 bonus on top of a much higher presence-only base, not a gate.
  - Tag data is trusted somewhat more than txt data when neither is
    DB-matched (0.70 vs 0.55 presence base) — a raw tag field is more
    reliable to extract than a regex/fuzzy guess out of a prose text file.
    Once DB-matched, source doesn't matter (0.20 bonus either way) — a
    confirmed match is a confirmed match regardless of which pipeline
    produced the candidate string.
  - Date has no DB-match concept (nothing to check a date against). Ryan
    was explicit that date agreement must be all-or-nothing: the +0.30
    corroboration bonus ONLY fires when both tag and txt carry full
    day-precision dates that are byte-identical. A coarser "same year and
    month" agreement earns nothing — there is real harm in implying a date
    match when it's merely close, since a taper's info file describing a
    DIFFERENT nearby date would otherwise look "confirmed."
  - Track titles get a from-scratch, five-state model (Confirmed / Tag-only
    / Txt-only / Conflict / Missing) rather than a binary "has a title."
    Ryan's real-world calibration: only ~5% of recordings "in the wild"
    will ever hit Confirmed (both sources present AND agreeing) — so
    Tag-only alone is scored as solidly good (0.75), not treated as an
    incomplete/penalized state.

Both scores are DB-match-aware but this module itself is DB-free — the
caller (app/api/recordings.py's scan endpoint) fetches known artist
names and known venue records (name + city + state + country) and passes
them in, the same pattern app.utils.ingest.parse_info_file already
established for its own (currently unused in production) fuzzy matching.
"""

from app.utils.ingest import _fuzzy_match
from app.utils.health import _is_real_title

# ── Weights (sum to 100 so the Primary Attribute Score reads 0-100 directly) ──
# Country is deliberately the lowest weight — domestic US shows (the bulk of
# a typical collection) essentially never state "US" explicitly in tags or
# text, so this keeps an otherwise-perfect domestic show from being punished
# hard for a field nobody fills in.
WEIGHTS = {
    "artist":  28,
    "date":       27,
    "venue_name": 20,
    "city":       12,
    "state":       8,
    "country":     5,
}

# Per-attribute component constants (Artist / Venue Name / City / State / Country)
_PRESENCE_TAG  = 0.70   # tag field has a real value
_PRESENCE_TXT  = 0.55   # only the info file has a value (no tag)
_MATCH_BONUS   = 0.20   # the winning value is DB-matched (>=0.85 fuzzy), either source
_AGREE_BONUS   = 0.10   # both sources present AND agree with each other

# Date component constants — same shape, precision substitutes for "match"
_DATE_TAG_MAX     = 0.70   # tag date at full (day) precision
_DATE_TXT_MAX     = 0.55   # txt date at full (day) precision, tag absent
_DATE_AGREE_BONUS = 0.30   # both full day-precision AND byte-identical — all or nothing

# Track completeness — per-track point value by state. Calibrated so a
# "Tag-only" recording (the common case) still reads as solidly good, since
# full "Confirmed" corroboration is realistically rare (~5% per Ryan).
_TRACK_POINTS = {
    "confirmed": 1.00,
    "tag_only":  0.75,
    "txt_only":  0.60,
    "conflict":  0.55,
    "missing":   0.00,
}


def _present(v):
    return bool(v and str(v).strip())


def _values_agree(a, b):
    """Fuzzy-agree (same 0.85 cutoff used for DB matching) — used to compare
    a tag value directly against a txt value, not against the DB."""
    if not _present(a) or not _present(b):
        return False
    return _fuzzy_match(str(a), [str(b)]) is not None


def _score_name_attribute(tag_value, tag_matched, txt_value, txt_matched):
    """
    Score one of Artist / Venue Name / City / State / Country.
    Returns (subscore 0-1, detail dict) — detail carries the raw flags so
    the debug panel / UI can show exactly what produced the number.
    """
    tag_present = _present(tag_value)
    txt_present = _present(txt_value)

    base = _PRESENCE_TAG if tag_present else (_PRESENCE_TXT if txt_present else 0.0)
    matched = (tag_present and tag_matched) or (txt_present and txt_matched)
    match_bonus = _MATCH_BONUS if matched else 0.0

    agree = tag_present and txt_present and _values_agree(tag_value, txt_value)
    agree_bonus = _AGREE_BONUS if agree else 0.0

    # Rounded here (not just at the top-level caller) so float noise from
    # adding 0.70+0.20+0.10 etc. (e.g. 0.7999999999999999) never leaks into
    # the debug panel or a direct caller of this helper.
    subscore = round(min(1.0, base + match_bonus + agree_bonus), 2)

    detail = {
        "tag_value":   tag_value or None,
        "tag_present": tag_present,
        "tag_matched": bool(tag_present and tag_matched),
        "txt_value":   txt_value or None,
        "txt_present": txt_present,
        "txt_matched": bool(txt_present and txt_matched),
        "agree":       agree,
        "components": {
            "presence": round(base, 2),
            "match_bonus": round(match_bonus, 2),
            "agree_bonus": round(agree_bonus, 2),
        },
    }
    return subscore, detail


def _match_venue(value, known_venues):
    """known_venues: [{'name':, 'city':, 'state':, 'country':}, ...].
    Returns the matched venue dict, or None."""
    if not _present(value) or not known_venues:
        return None
    names = [v["name"] for v in known_venues if v.get("name")]
    matched_name = _fuzzy_match(str(value), names)
    if not matched_name:
        return None
    for v in known_venues:
        if v.get("name") == matched_name:
            return v
    return None


def _location_matched(value, field, winning_venue):
    """Does this tag/txt-inferred city/state/country agree with the field
    on the venue record we already resolved by name? Nothing to check
    against (new/unmatched venue) => always False, not a penalty — just
    means this component can't earn the match bonus, same as any other
    'nothing to corroborate against yet' case."""
    if not _present(value) or not winning_venue:
        return False
    ref = winning_venue.get(field)
    if not _present(ref):
        return False
    return _values_agree(value, ref)


def _parse_tag_date(concert_date):
    """'YYYY-MM-DD' / 'YYYY-MM' / 'YYYY' (or falsy) -> (year, month, day)."""
    if not concert_date:
        return (None, None, None)
    parts = str(concert_date).split("-")
    def _int_or_none(s):
        s = (s or "").strip()
        return int(s) if s.isdigit() else None
    y = _int_or_none(parts[0]) if len(parts) > 0 else None
    m = _int_or_none(parts[1]) if len(parts) > 1 else None
    d = _int_or_none(parts[2]) if len(parts) > 2 else None
    return (y, m, d)


def _date_precision(ymd):
    y, m, d = ymd
    if d: return 3
    if m: return 2
    if y: return 1
    return 0


def _score_date(tag_ymd, txt_ymd):
    tag_prec = _date_precision(tag_ymd)
    txt_prec = _date_precision(txt_ymd)

    if tag_prec:
        base = (tag_prec / 3.0) * _DATE_TAG_MAX
    elif txt_prec:
        base = (txt_prec / 3.0) * _DATE_TXT_MAX
    else:
        base = 0.0

    # All-or-nothing per Ryan's call: only a full day-precision, byte-identical
    # match on BOTH sides earns the bonus. A coarser "same year/month" agreement
    # earns nothing — implying a date match when it's merely close is worse
    # than showing no corroboration at all.
    exact_agree = (tag_prec == 3 and txt_prec == 3 and tag_ymd == txt_ymd)
    bonus = _DATE_AGREE_BONUS if exact_agree else 0.0

    subscore = round(min(1.0, base + bonus), 2)
    detail = {
        "tag_date": tag_ymd if tag_prec else None,
        "txt_date": txt_ymd if txt_prec else None,
        "tag_precision": tag_prec,
        "txt_precision": txt_prec,
        "exact_agree": exact_agree,
        "components": {
            "presence": round(base, 2),
            "agree_bonus": round(bonus, 2),
        },
    }
    return subscore, detail


def _score_tracks(tag_tracks, txt_tracks, n_audio):
    """
    tag_tracks: from_tags.tracks — list ordered by audio index, each {"title": ...}
    txt_tracks: from_info_file.tracks — list of {"number":, "title":}
    """
    txt_by_num = {t.get("number"): t.get("title") for t in (txt_tracks or [])}

    breakdown = {"confirmed": 0, "tag_only": 0, "txt_only": 0, "conflict": 0, "missing": 0}
    tracks_detail = []
    total_points = 0.0

    for i in range(n_audio):
        idx = i + 1
        tag_title = tag_tracks[i].get("title") if i < len(tag_tracks) else None
        txt_title = txt_by_num.get(idx)

        tag_real = _is_real_title(tag_title)
        txt_real = _is_real_title(txt_title)

        if tag_real and txt_real:
            state = "confirmed" if _values_agree(tag_title, txt_title) else "conflict"
        elif tag_real:
            state = "tag_only"
        elif txt_real:
            state = "txt_only"
        else:
            state = "missing"

        breakdown[state] += 1
        total_points += _TRACK_POINTS[state]
        tracks_detail.append({
            "index": idx, "state": state,
            "tag_title": tag_title or None, "txt_title": txt_title or None,
        })

    score = round(100 * total_points / n_audio) if n_audio else 0
    return {"score": score, "breakdown": breakdown, "tracks": tracks_detail}


def compute_paula_score(scan, known_artists=None, known_venues=None):
    """
    scan: a build_scan_payload()-shaped dict.
    known_artists: list of artist name strings.
    known_venues: list of {"name":, "city":, "state":, "country":} dicts.

    Returns:
      {
        "score": int 0-100,                      # Primary Attribute Score
        "attributes": {artist, date, venue_name, city, state, country},
        "track_completeness": {score, breakdown, tracks},
      }
    Every "attributes" entry carries its raw flags/components/subscore/
    weight/points — this is intentionally verbose so the debug panel can
    show exactly what produced the final number, not just the number itself.
    """
    known_artists = known_artists or []
    known_venues = known_venues or []

    sugg = scan.get("suggestions") or {}
    tags = sugg.get("from_tags") or {}
    info = sugg.get("from_info_file") or {}

    # Artist
    tag_artist = tags.get("artist")
    txt_artist = info.get("artist")
    tag_perf_matched = bool(tag_artist and _fuzzy_match(str(tag_artist), known_artists))
    txt_perf_matched = bool(txt_artist and _fuzzy_match(str(txt_artist), known_artists))
    perf_sub, perf_detail = _score_name_attribute(
        tag_artist, tag_perf_matched, txt_artist, txt_perf_matched)

    # Venue Name (+ resolve the winning venue record for location cross-checks)
    tag_venue = tags.get("venue")
    txt_venue = info.get("venue")
    tag_venue_rec = _match_venue(tag_venue, known_venues)
    txt_venue_rec = _match_venue(txt_venue, known_venues)
    winning_venue = tag_venue_rec or txt_venue_rec   # tag wins ties, per Ryan's rule #3
    venue_sub, venue_detail = _score_name_attribute(
        tag_venue, bool(tag_venue_rec), txt_venue, bool(txt_venue_rec))

    # City / State / Country — "matched" = agrees with the winning venue's stored value
    def _loc(field):
        tag_val = tags.get(field)
        txt_val = info.get(field)
        tag_m = _location_matched(tag_val, field, winning_venue)
        txt_m = _location_matched(txt_val, field, winning_venue)
        return _score_name_attribute(tag_val, tag_m, txt_val, txt_m)

    city_sub, city_detail       = _loc("city")
    state_sub, state_detail     = _loc("state")
    country_sub, country_detail = _loc("country")

    # Date
    tag_ymd = _parse_tag_date(tags.get("concert_date"))
    txt_ymd = (info.get("year"), info.get("month"), info.get("day"))
    date_sub, date_detail = _score_date(tag_ymd, txt_ymd)

    subscores = {
        "artist":  (perf_sub, perf_detail),
        "date":       (date_sub, date_detail),
        "venue_name": (venue_sub, venue_detail),
        "city":       (city_sub, city_detail),
        "state":      (state_sub, state_detail),
        "country":    (country_sub, country_detail),
    }

    attributes_out = {}
    total_points = 0.0
    for name, (sub, detail) in subscores.items():
        weight = WEIGHTS[name]
        points = sub * weight
        total_points += points
        attributes_out[name] = {
            **detail,
            "subscore": round(sub, 3),
            "weight":   weight,
            "points":   round(points, 2),
        }

    score = max(0, min(100, round(total_points)))

    # Track completeness
    n_audio = scan.get("audio_file_count") or len(scan.get("audio_files") or [])
    tag_tracks = tags.get("tracks") or []
    txt_tracks = info.get("tracks") or []
    track_result = _score_tracks(tag_tracks, txt_tracks, n_audio)

    return {
        "score": score,
        "attributes": attributes_out,
        "track_completeness": track_result,
    }
