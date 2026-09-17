"""
tests/test_paula.py — pure-function unit tests for app/utils/paula.py.

Paula is the free, non-AI completeness/confidence scorer (2026-07-16 design
conversation with Ryan). No DB or app context needed — known_artists/
known_venues are passed in directly, same pattern as parse_info_file's own
(currently unused-in-production) fuzzy matching.
"""

from app.utils.paula import compute_paula_score, _score_name_attribute, _score_date


def _scan(tags=None, info=None, audio_count=1):
    return {
        "audio_file_count": audio_count,
        "audio_files": [{"filename": f"{i:02d}.flac"} for i in range(1, audio_count + 1)],
        "suggestions": {"from_tags": tags or {}, "from_info_file": info or {}},
    }


# ── Name-like attribute component math ──────────────────────────────────────

def test_name_attribute_full_agreement_scores_max():
    sub, detail = _score_name_attribute("Bill Evans", True, "Bill Evans", True)
    assert sub == 1.0
    assert detail["tag_matched"] and detail["txt_matched"] and detail["agree"]


def test_name_attribute_new_unmatched_tag_alone_scores_070():
    """The calibration fix Ryan asked for: a well-formed tag value that just
    hasn't been seen in the DB yet (~10% of ingests, per Ryan) should NOT
    score as unreliable. 0.70, not the old 0.35."""
    sub, detail = _score_name_attribute("Brand New Act", False, None, False)
    assert sub == 0.70
    assert detail["tag_present"] and not detail["tag_matched"]


def test_name_attribute_txt_only_matched_beats_txt_only_unmatched():
    matched_sub, _   = _score_name_attribute(None, False, "Fillmore East", True)
    unmatched_sub, _ = _score_name_attribute(None, False, "Fillmore East", False)
    assert matched_sub == 0.75          # 0.55 presence + 0.20 match
    assert unmatched_sub == 0.55
    assert matched_sub > unmatched_sub


def test_name_attribute_db_match_is_symmetric_across_source():
    """A DB match via txt-only should score the same presence+match total as
    tag-only+match — a confirmed match is a confirmed match regardless of
    which pipeline produced the candidate string (this is what fixed the
    'sparse tags, rich info file' case scoring too low)."""
    tag_matched_sub, _ = _score_name_attribute("X", True, None, False)
    txt_matched_sub, _ = _score_name_attribute(None, False, "X", True)
    assert tag_matched_sub == 0.90       # 0.70 + 0.20
    assert txt_matched_sub == 0.75       # 0.55 + 0.20 (txt presence is lower, not the match bonus)


def test_name_attribute_disagreement_gets_no_agreement_bonus_but_no_penalty():
    agree_sub, _    = _score_name_attribute("Fillmore", False, "Fillmore", False)
    disagree_sub, _ = _score_name_attribute("Fillmore", False, "The Warehouse", False)
    assert agree_sub == 0.80      # 0.70 + 0.10 agree bonus
    assert disagree_sub == 0.70   # tag's presence only — no bonus, no extra penalty


def test_name_attribute_nothing_present_scores_zero():
    sub, detail = _score_name_attribute(None, False, None, False)
    assert sub == 0.0
    assert not detail["tag_present"] and not detail["txt_present"]


# ── Date: all-or-nothing agreement (Ryan's explicit correction) ────────────

def test_date_full_agreement_gets_bonus():
    sub, detail = _score_date((1979, 12, 12), (1979, 12, 12))
    assert sub == 1.0
    assert detail["exact_agree"]


def test_date_tag_full_txt_month_only_gets_no_bonus():
    """The exact false-positive Ryan flagged: txt only says 'December 1979'
    (no day) — under the old shared-precision rule this wrongly earned the
    full agreement bonus. Must now score tag-presence-only."""
    sub, detail = _score_date((1979, 12, 12), (1979, 12, None))
    assert sub == 0.70          # tag's own day-precision presence only
    assert not detail["exact_agree"]


def test_date_full_both_but_different_day_gets_no_bonus():
    sub, detail = _score_date((1979, 12, 12), (1979, 12, 11))
    assert sub == 0.70
    assert not detail["exact_agree"]


def test_date_txt_only_full_precision_no_tag():
    sub, detail = _score_date((None, None, None), (1979, 12, 12))
    assert sub == 0.55           # (3/3) * 0.55, no tag to bonus against
    assert detail["tag_date"] is None


def test_date_nothing_present():
    sub, _ = _score_date((None, None, None), (None, None, None))
    assert sub == 0.0


# ── Full weighted score — locks in the worked examples Ryan reviewed ───────

def test_full_score_well_tagged_new_artist_known_venue():
    """Reproduces the worked example Ryan approved: well-tagged show, brand
    new (unmatched) artist, venue already in the DB. Should land ~75,
    not the old miscalibrated 58."""
    tags = {
        "artist": "Brand New Act", "concert_date": "1994-06-15",
        "venue": "The Fillmore", "city": "San Francisco", "state": "CA",
        "country": None,
    }
    scan = _scan(tags=tags, info={})
    known_venues = [{"name": "The Fillmore", "city": "San Francisco", "state": "CA", "country": "US"}]
    result = compute_paula_score(scan, known_artists=[], known_venues=known_venues)
    assert 73 <= result["score"] <= 77
    assert result["attributes"]["artist"]["subscore"] == 0.70
    assert result["attributes"]["venue_name"]["subscore"] == 0.90   # 0.70 + 0.20 match


def test_full_score_best_case_domestic_caps_below_100():
    """Everything corroborated, but Country is never mentioned (typical
    domestic show) — score should land high but not hit 100, and Country's
    own subscore should be exactly 0."""
    tags = {
        "artist": "Bill Evans Trio", "concert_date": "1980-02-22",
        "venue": "Sprague Hall", "city": "New Haven", "state": "CT", "country": None,
    }
    # from_info_file carries year/month/day as separate ints, not a
    # "concert_date" string like from_tags does.
    info = {
        "artist": "Bill Evans Trio", "year": 1980, "month": 2, "day": 22,
        "venue": "Sprague Hall", "city": "New Haven", "state": "CT", "country": None,
    }
    scan = _scan(tags=tags, info=info)
    known_artists = ["Bill Evans Trio"]
    known_venues = [{"name": "Sprague Hall", "city": "New Haven", "state": "CT", "country": "US"}]
    result = compute_paula_score(scan, known_artists, known_venues)
    assert result["attributes"]["country"]["subscore"] == 0.0
    assert 90 <= result["score"] < 100


def test_full_score_nothing_present_is_zero():
    result = compute_paula_score(_scan(tags={}, info={}), [], [])
    assert result["score"] == 0
    assert result["track_completeness"]["score"] == 0


# ── Track completeness ──────────────────────────────────────────────────────

def test_track_completeness_confirmed_tag_only_txt_only_conflict_missing():
    tags = {"tracks": [
        {"title": "Dark Star"},        # will agree with txt -> confirmed
        {"title": "St. Stephen"},      # no txt entry -> tag_only
        {"title": None},               # only txt has it -> txt_only
        {"title": "Truckin"},          # txt disagrees -> conflict
        {"title": None},               # neither -> missing
    ]}
    info = {"tracks": [
        {"number": 1, "title": "Dark Star"},
        {"number": 3, "title": "The Other One"},
        {"number": 4, "title": "Not Fade Away"},
    ]}
    scan = _scan(tags=tags, info=info, audio_count=5)
    result = compute_paula_score(scan, [], [])
    bd = result["track_completeness"]["breakdown"]
    assert bd == {"confirmed": 1, "tag_only": 1, "txt_only": 1, "conflict": 1, "missing": 1}
    # (1.00 + 0.75 + 0.60 + 0.55 + 0.00) / 5 * 100 = 58
    assert result["track_completeness"]["score"] == 58


def test_track_completeness_all_tag_only_still_scores_solidly():
    """Per Ryan's real-world calibration note (~5% full-match rate), a
    recording where every track is tag-only (no info file at all) should
    still read as a good score, not penalized for lacking corroboration."""
    tags = {"tracks": [{"title": "One"}, {"title": "Two"}, {"title": "Three"}]}
    scan = _scan(tags=tags, info={}, audio_count=3)
    result = compute_paula_score(scan, [], [])
    assert result["track_completeness"]["score"] == 75
    assert result["track_completeness"]["breakdown"]["tag_only"] == 3
