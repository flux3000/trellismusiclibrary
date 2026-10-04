"""Segmenter and place peeler (Resolver v2 chunk 2, 2026-10-03)."""
import pytest

from app.utils.ingest import _parse_location, _parse_location_plain, parse_info_file
from app.utils.reader.place import peel
from app.utils.reader.segment import segment_line, segment_text


def _venue(line):
    return parse_info_file(None, text="Some Artist\n" + line + "\n")["venue"]


# ── segmenter ────────────────────────────────────────────────────────────────

def test_segment_separators_and_spans():
    line = "Newport, RI @ Ft. Adams State Park - Newport Jazz Fest - Harbor Stage"
    segs = segment_line(line)
    assert [s.text for s in segs] == ["Newport", "RI", "Ft. Adams State Park",
                                      "Newport Jazz Fest", "Harbor Stage"]
    assert [s.hard_before for s in segs] == [False, False, True, True, True]
    for s in segs:
        assert line[s.start:s.end] == s.text


def test_segment_comma_without_space_and_digits():
    assert [s.text for s in segment_line("BLUES ALLEY,WASH.D.C.")] == ["BLUES ALLEY", "WASH.D.C."]
    assert [s.text for s in segment_line("1,000 people, Boston")] == ["1,000 people", "Boston"]


def test_segment_label_lines_and_blocks():
    text = "Venue: Fillmore East; New York, NY\n\nSet 1:\nSource: SBD"
    segs = segment_text(text)
    assert segs[0].label == "Venue" and segs[0].text == "Fillmore East"
    assert segs[0].line == 0 and segs[0].block == 0
    assert [s.text for s in segs if s.line == 0] == ["Fillmore East", "New York", "NY"]
    last = segs[-1]
    assert last.label == "Source" and last.text == "SBD" and last.block == 1 and last.line == 3
    assert text[last.start:last.end] == "SBD"


# ── peeler ───────────────────────────────────────────────────────────────────

def test_peel_starr_hill():
    r = peel("Starr Hill Music Hall, Charlottesville VA")
    assert (r.left, r.city, r.region, r.country) == ("Starr Hill Music Hall", "Charlottesville", "VA", "US")
    assert r.confidence == "high"
    assert [e["kind"] for e in r.evidence] == ["region", "city"]
    assert _venue("Starr Hill Music Hall, Charlottesville VA") == "Starr Hill Music Hall"


def test_peel_at_sign_venue_to_the_right():
    r = peel("Forest Hills, NY @ Forest Hills Stadium")
    assert (r.city, r.region, r.country) == ("Forest Hills", "NY", "US")
    assert r.venue_candidate() == "Forest Hills Stadium"
    assert _venue("Forest Hills, NY @ Forest Hills Stadium") == "Forest Hills Stadium"


def test_peel_city_only_has_no_country():
    # Documented: a bare gazetteer city gives the city, low confidence, and no country.
    r = peel("Royal Albert Hall, London")
    assert (r.city, r.region, r.country, r.left) == ("London", "", "", "Royal Albert Hall")
    assert r.confidence == "low"
    assert _venue("Royal Albert Hall, London") == "Royal Albert Hall"
    # ... and the plain wrapper does not call that a location line
    assert _parse_location_plain("Royal Albert Hall, London") == (None, None, None)


def test_peel_canadian_province():
    r = peel("The Hummingbird Centre, Toronto, ON")
    assert (r.left, r.city, r.region, r.country) == ("The Hummingbird Centre", "Toronto", "Ontario", "Canada")
    # abbreviations are read as written: lower-case "on" needs the city to be in Ontario
    assert peel("Toronto, on").region == "Ontario"
    assert peel("Set on").has_region_or_country is False
    assert _parse_location("The Hummingbird Centre, Toronto, ON") == ("Toronto", "ON", "Canada")


def test_peel_iso2_country():
    r = peel("Paris, FR")
    assert (r.city, r.region, r.country) == ("Paris", "", "France")
    assert peel("SBD, FR").has_region_or_country is False   # no city to vouch for the code


def test_peel_washington_dc_and_date_blanking():
    r = peel("BLUES ALLEY,WASH.D.C. JULY 11,1981")
    assert (r.city, r.region, r.country, r.left) == ("Washington", "DC", "US", "BLUES ALLEY")
    assert _venue("BLUES ALLEY,WASH.D.C. JULY 11,1981") == "Blues Alley"
    for line in ("Washington DC", "Washington, D.C.", "Wash. D.C."):
        assert peel(line).region == "DC" and peel(line).city == "Washington", line


def test_peel_dash_venue_and_long_country():
    r = peel("Stars – Philadelphia, Pennsylvania, USA")
    assert (r.city, r.region, r.country) == ("Philadelphia", "PA", "US")
    assert r.venue_candidate() == "Stars"
    assert _venue("Stars – Philadelphia, Pennsylvania, USA") == "Stars"


@pytest.mark.parametrize("line,expected", [
    ("New York, NY", ("New York", "NY", "US")),
    ("Ann Arbor MI", ("Ann Arbor", "MI", "US")),
    ("New York, NY, USA", ("New York", "NY", "US")),
    ("London, England", ("London", None, "UK")),
    ("London, U.K.", ("London", None, "UK")),
    ("Osaka, Japan", ("Osaka", None, "Japan")),
    ("Amsterdam, Netherlands", ("Amsterdam", None, "Netherlands")),
    ("Atlanta, Georgia", ("Atlanta", "GA", "US")),
    ("Tbilisi, Georgia", ("Tbilisi", None, "Georgia")),
    ("Lyons, Co", ("Lyons", "CO", "US")),
    ("Ottawa, Ontario (Canada)", ("Ottawa", "ON", "Canada")),
    ("Munich, Bavaria, Germany", ("Munich", None, "Germany")),
    ("Perth, WA, Australia", ("Perth", "WA", "Australia")),
    ("Rising Sun, MD", ("Rising Sun", "MD", "US")),
    ("East Hempstead, NY", ("East Hempstead", "NY", "US")),
])
def test_location_wrapper(line, expected):
    assert _parse_location_plain(line) == expected


@pytest.mark.parametrize("line", ["SBD", "Set 1", "PAT  METHENY  SIDE EYE III+  (USA)",
                                  "D. G. Q. Reunion / Dawg Jam", "JazzFest Berlin 2001",
                                  "Flight of the Bumblebee", ""])
def test_not_a_place(line):
    assert _parse_location_plain(line) == (None, None, None)


def test_venue_no_longer_swallows_city():
    assert _venue("Concertgebouw, Amsterdam, Netherlands") == "Concertgebouw"
    # a festival-only line is an EVENT now (Resolver v2 chunk 3), not a venue
    r = parse_info_file(None, text="Some Artist\nBoxcar Pinion Memorial Bluegrass Festival  Chattanooga, TN\n")
    assert r["event"] == "Boxcar Pinion Memorial Bluegrass Festival"
    assert r["venue"] is None


# ── artist line is never a place; lead-ins; NYC ─────────────────────────────

@pytest.mark.parametrize("artist", ["Boston", "Phoenix", "Santana", "Chicago"])
def test_artist_line_is_not_a_place(artist):
    r = parse_info_file(None, text=f"{artist}\nOakland Coliseum Arena\n1977-05-08\n")
    assert r["city"] is None and r["state"] is None and r["country"] is None
    assert r["venue"] == "Oakland Coliseum Arena"


def test_artist_line_region_word_is_not_state():
    r = parse_info_file(None, text="Kansas | Live\n1977-05-08\nSomewhere Hall, Topeka, KS\n")
    assert (r["city"], r["state"]) == ("Topeka", "KS")
    r = parse_info_file(None, text="Kansas | Live\n1977-05-08\n")
    assert r["state"] is None and r["country"] is None


def test_pipe_separator_and_venue_not_city():
    assert [s.text for s in segment_line("Warren Haynes | Chicago")] == ["Warren Haynes", "Chicago"]
    r = parse_info_file(None, text="Some Artist\nWarren Haynes | Chicago\n")
    assert r["venue"] != r["city"]


def test_live_in_lead_and_mass():
    assert _parse_location_plain("Live in Boston, Mass.") == ("Boston", "MA", "US")
    assert _parse_location_plain("Boston, Mass") == ("Boston", "MA", "US")
    assert _venue("Live in Boston, Mass.") is None
    assert _venue("Live at The Orpheum, Boston, MA") == "The Orpheum"


@pytest.mark.parametrize("line", ["NYC", "New York City", "Central Park NYC NY.", "Nyc, NY"])
def test_nyc(line):
    r = peel(line)
    assert (r.city, r.region, r.country) == ("New York", "NY", "US")


def test_state_is_filled_for_canada_and_australia_only():
    assert _parse_location("Toronto, ON") == ("Toronto", "ON", "Canada")
    assert _parse_location("Sydney, NSW, Australia") == ("Sydney", "NSW", "Australia")
    assert _parse_location("Munich, Bavaria, Germany")[1] in ("", None)
    assert _parse_location("Paris, France")[1] in ("", None)


def test_common_city_abbreviations_read_as_the_city():
    from app.utils.reader.place import peel
    for text, city, st in (("Fillmore, SF", "San Francisco", "CA"), ("Warfield, SF, CA", "San Francisco", "CA"),
                           ("Troubadour, L.A.", "Los Angeles", "CA"), ("Tipitina's, Nola", "New Orleans", "LA"),
                           ("Electric Factory, Philly", "Philadelphia", "PA")):
        r = peel(text, False)
        assert (r.city, r.state, r.country) == (city, st, "US"), text
    assert peel("Baton Rouge, LA", False).city == "Baton Rouge"      # LA alone stays Louisiana
