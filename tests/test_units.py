"""
tests/test_units.py — pure-function unit tests (no DB needed).
"""

from app.utils.format import format_partial_date
from app.utils.ingest import detect_track_flags, _parse_location
from app.utils.health import compute_health
from app.utils.waveform import downsample_peaks


# ── compute_health ────────────────────────────────────────────────────────────

def _scan(tags=None, info=None, audio=None, info_content="x"):
    """Minimal scan payload builder for health tests."""
    audio = audio if audio is not None else [{"filename": f"Track {i:02d}.flac"} for i in range(1, 3)]
    return {
        "audio_file_count": len(audio),
        "audio_files": audio,
        "info_file_content": info_content,
        "suggestions": {"from_tags": tags or {}, "from_info_file": info or {}},
    }


def _flac(n):
    return [{"filename": f"Track {i:02d}.flac"} for i in range(1, n + 1)]


def _full_core(**over):
    """A fully-populated set of core fields (all 8 present, full date)."""
    base = {"artist": "X", "concert_date": "1980-05-08", "venue": "Fillmore",
            "city": "New York", "state": "NY", "country": "US",
            "source": "SBD", "lineage": "SBD>DAT"}
    base.update(over)
    return base


def test_health_clean_show_is_green():
    # All 8 core fields populated + every track has a real title → 100.
    tags = _full_core(artist="Billy Joel", concert_date="1993-07-01",
                      tracks=[{"track_number": i, "title": f"Song {i}"} for i in range(1, 18)])
    info = {"artist": "Billy Joel", "year": 1993, "month": 7, "day": 1, "venue": "Logan Hall",
            "city": "London", "state": "", "country": "UK",
            "tracks": [{"number": i, "title": f"Song {i}"} for i in range(1, 18)]}
    h = compute_health(_scan(tags, info, _flac(17)))
    assert h["band"] == "green"
    assert h["score"] == 100


def test_health_placeholder_titles_do_not_count():
    # THE core bug: "Track 01"-style titles are NOT real titles. All 8 core fields
    # present but 0/10 real titles → 8 of 18 fields ≈ 44 → red, not green.
    tags = _full_core(artist="Bela Fleck",
                      tracks=[{"track_number": i, "title": f"Track {i:02d}"} for i in range(1, 11)])
    info = dict(tags, year=1980, month=5, day=8, tracks=[])
    h = compute_health(_scan(tags, info, _flac(10)))
    assert h["band"] == "red"
    assert h["score"] < 60
    assert any("lack a real title" in f["msg"] for f in h["factors"])


def test_health_partial_tracks_named():
    # 8 core + 5/10 real titles → 13 of 18 ≈ 72 → yellow.
    titles = [f"Song {i}" if i <= 5 else f"Track {i:02d}" for i in range(1, 11)]
    tags = _full_core(tracks=[{"track_number": i, "title": titles[i - 1]} for i in range(1, 11)])
    h = compute_health(_scan(tags, tags, _flac(10)))
    assert h["band"] == "yellow"
    assert any("5 of 10 tracks lack a real title" in f["msg"] for f in h["factors"])


def test_health_missing_core_field_detracts():
    # Drop Lineage from an otherwise-perfect show → one field lost.
    tags = _full_core(lineage="", tracks=[{"track_number": 1, "title": "Intro"}])
    full = _full_core(tracks=[{"track_number": 1, "title": "Intro"}])
    h_missing = compute_health(_scan(tags, tags, _flac(1)))
    h_full    = compute_health(_scan(full, full, _flac(1)))
    assert h_full["score"] > h_missing["score"]
    assert any(f["msg"] == "No lineage" for f in h_missing["factors"])


def test_health_date_precision_graded():
    year_only = _full_core(concert_date="1980", tracks=[{"track_number": 1, "title": "Intro"}])
    y = dict(year_only); y.pop("concert_date"); y["year"] = 1980
    full = _full_core(tracks=[{"track_number": 1, "title": "Intro"}])
    h_year = compute_health(_scan(year_only, y, _flac(1)))
    h_full = compute_health(_scan(full, full, _flac(1)))
    assert h_year["score"] < h_full["score"]
    assert any("Year only" in f["msg"] for f in h_year["factors"])


def test_health_no_audio_is_zero():
    h = compute_health(_scan({}, {}, audio=[]))
    assert h["score"] == 0 and h["band"] == "red"


def test_health_exposes_track_and_date_detail_for_the_metadata_panel():
    """The Metadata Quality panel reads these fields directly rather than parsing
    the human-readable factor strings. If they stop matching the factors, the
    panel and the score start telling different stories on the same card — so
    pin them against the message they duplicate.
    """
    titles = [f"Song {i}" if i <= 5 else f"Track {i:02d}" for i in range(1, 11)]
    tags = _full_core(concert_date="1980-05",
                      tracks=[{"track_number": i, "title": titles[i - 1]} for i in range(1, 11)])
    info = dict(tags); info.pop("concert_date"); info.update(year=1980, month=5)
    h = compute_health(_scan(tags, info, _flac(10)))

    assert h["tracks_named"] == 5 and h["tracks_total"] == 10
    assert any("5 of 10 tracks lack a real title" in f["msg"] for f in h["factors"])
    # Y-M, no day → precision 2, matching the "Date missing day" factor.
    assert h["date_precision"] == 2
    assert any(f["msg"] == "Date missing day" for f in h["factors"])


def test_band_labels_are_neutral():
    from app.utils.quality import BAND_LABEL
    assert set(BAND_LABEL.values()) == {"High", "Medium", "Low"}


def test_parse_location_multiword_city_not_truncated():
    # Regression: "New York" must not become "York".
    assert _parse_location("New York, NY") == ("New York", "NY", "US")
    assert _parse_location("New York, NY, USA") == ("New York", "NY", "US")
    assert _parse_location("San Francisco, CA") == ("San Francisco", "CA", "US")

def test_parse_location_drops_venue_prefix():
    assert _parse_location("Fillmore East, New York, NY") == ("New York", "NY", "US")

def test_parse_location_countries_and_aliases():
    assert _parse_location("Osaka, Japan") == ("Osaka", None, "Japan")
    assert _parse_location("London, UK") == ("London", None, "UK")
    assert _parse_location("London, England") == ("London", None, "UK")

def test_parse_location_no_comma():
    assert _parse_location("Ann Arbor MI") == ("Ann Arbor", "MI", "US")

def test_parse_location_rejects_non_location():
    for line in ["SBD", "Soundboard recording", "Set 1", ""]:
        assert _parse_location(line) == (None, None, None), line


def test_format_partial_date_full():
    assert format_partial_date(1977, 5, 8) == "1977-05-08"

def test_format_partial_date_month():
    assert format_partial_date(1977, 5, None) == "1977-05"

def test_format_partial_date_year():
    assert format_partial_date(1977, None, None) == "1977"

def test_format_partial_date_none():
    assert format_partial_date(None, 5, 8) is None


def test_flags_structural_markers():
    assert detect_track_flags("// Dark Star") == ["start_truncated"]
    assert detect_track_flags("Dark Star //") == ["end_truncated"]
    assert detect_track_flags("Dark Star (x)") == ["incomplete"]

def test_flags_keyword_segments():
    assert detect_track_flags("tuning") == ["tuning"]
    assert detect_track_flags("tuning and banter (Bobby)") == ["banter", "tuning"]
    assert detect_track_flags("Band Intros") == ["band_intros"]
    assert detect_track_flags("Announcement") == ["announcement"]

def test_flags_banter_synonyms():
    assert detect_track_flags("Chatter") == ["banter"]
    assert detect_track_flags("Crosstalk") == ["banter"]

def test_flags_no_false_positives_on_real_titles():
    # Musical segues / real songs must NOT be flagged
    for title in ["Speak Low", "Don't Talk (Put Your Head On My Shoulder)",
                  "Piano Intro >", "Dark Star Intro -> Fields of Gray",
                  "Cryptical Envelopment >"]:
        assert detect_track_flags(title) == [], title



def test_downsample_peaks_empty_input_is_flat_zero():
    assert downsample_peaks([], 10) == [0.0] * 10
    assert downsample_peaks(None, 10) == [0.0] * 10
    assert downsample_peaks({}, 10) == [0.0] * 10
    assert downsample_peaks({"min": [], "max": []}, 10) == [0.0] * 10


def test_downsample_peaks_zero_n_is_empty():
    assert downsample_peaks([0.1, 0.2, 0.3], 0) == []


def test_downsample_peaks_short_input_upsamples_to_n():
    # Fewer source points than requested buckets — every bucket must still be
    # filled (a short input must not produce a short strip).
    result = downsample_peaks([0.2, 0.8], 10)
    assert len(result) == 10
    assert set(result) <= {0.2, 0.8}
    assert result[0] == 0.2 and result[-1] == 0.8


def test_downsample_peaks_exact_length_is_passthrough_magnitude():
    peaks = [-0.1, 0.4, -0.9, 0.05, -0.5]
    result = downsample_peaks(peaks, 5)
    assert result == [abs(v) for v in peaks]


def test_downsample_peaks_long_input_buckets_by_max_not_average():
    # 100 points → 10 buckets of 10. Each bucket keeps its loudest sample, not
    # the average — a waveform strip's job is to show the peak, not smooth it
    # away.
    peaks = [v / 100 for v in range(100)]   # monotonically increasing 0.00-0.99
    result = downsample_peaks(peaks, 10)
    assert len(result) == 10
    expected = [round((i * 10 + 9) / 100, 10) for i in range(10)]
    for got, exp in zip(result, expected):
        assert abs(got - exp) < 1e-9


def test_downsample_peaks_accepts_min_max_dict_shape():
    # The real (v2) TrackAnalysis.waveform_json shape: {"min": [...], "max": [...]},
    # signed -1..1. Magnitude per bucket is whichever extreme is larger.
    peaks = {"min": [-0.9, -0.1, -0.5, -0.2],
             "max": [0.3, 0.05, 0.6, 0.1]}
    result = downsample_peaks(peaks, 4)
    assert result == [0.9, 0.1, 0.6, 0.2]
