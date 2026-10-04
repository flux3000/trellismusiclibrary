"""
tests/test_reader_decode.py -- the Resolver v2 role decoder, billing assembler
and library index (app/utils/reader/{features,decode,billing,library,tracks}.py),
through parse_info_file().
"""
from pathlib import Path

import pytest

from app.utils.ingest import parse_info_file
from app.utils.reader.library import LibraryIndex
from app.utils.reader.tracks import align_durations, printed_seconds

FIX = Path(__file__).parent / "fixtures" / "resolver"
TRIO_ACT = "Al Di Meola, John McLaughlin and Paco de Lucía"
TRIO_MEMBERS = ["Al Di Meola", "John McLaughlin", "Paco de Lucía"]


def _trio_text():
    return (FIX / "guitar_trio.txt").read_text(encoding="utf-8", errors="replace")


def _read(text, **kw):
    return parse_info_file(None, text=text, **kw)


# ── Guitar Trio, end to end ──────────────────────────────────────────────────

def test_guitar_trio_assembles_the_artist_without_a_library():
    r = _read(_trio_text())
    assert (r["year"], r["month"], r["day"]) == (1983, 6, 18)
    assert r["venue"] == "Hammersmith Odeon"
    assert r["city"] == "London"
    assert r["country"] == "UK"
    assert r["source"] == "SBD"
    a = r["artist"]
    for name in ("McLaughlin", "Meola"):
        assert name in a
    assert " and " in a
    assert r["evidence"]["artist_tentative"] is True
    assert r["evidence"]["library"] is False


def test_guitar_trio_links_to_the_library_act():
    lib = LibraryIndex.from_dicts(artists=[{"name": TRIO_ACT, "members": TRIO_MEMBERS}])
    r = _read(_trio_text(), library=lib)
    assert r["artist"] == TRIO_ACT
    assert r["evidence"]["artist_tentative"] is False
    assert r["evidence"]["library"] is True


# ── Venue, event, stage ──────────────────────────────────────────────────────

def test_festival_line_is_an_event_not_a_venue():
    r = _read("Hot Rize\n6.20.1999\nTelluride Bluegrass Festival\nTelluride, CO\n")
    assert r["event"] == "Telluride Bluegrass Festival"
    assert r["venue"] is None
    assert (r["city"], r["state"]) == ("Telluride", "CO")


def test_stage_line_is_a_stage():
    r = _read("Jerry Douglas Band\n2003-07-18\nGrey Fox Bluegrass Festival\nHillside Stage\nAncramdale, NY\n")
    assert r["event"] == "Grey Fox Bluegrass Festival"
    assert r["stage"] == "Hillside Stage"


def test_library_event_supplies_the_venue():
    lib = LibraryIndex.from_dicts(
        venues=[{"name": "Town Park", "city": "Telluride", "state": "CO", "country": "US"}],
        events=[{"name": "Telluride Bluegrass Festival", "venue": "Town Park"}])
    r = _read("Hot Rize\n6.20.1999\nTelluride Bluegrass Festival\nTelluride, CO\n", library=lib)
    assert r["event"] == "Telluride Bluegrass Festival"
    assert r["venue"] == "Town Park"


def test_seldom_scene_on_line_two_is_the_artist():
    r = _read("1996-07-20\nSeldom Scene\nWinterhawk Bluegrass Festival\nAncramdale, NY\n")
    assert r["artist"] == "Seldom Scene"
    assert r["venue"] != "Seldom Scene"
    assert r["event"] == "Winterhawk Bluegrass Festival"


def test_a_lineage_line_is_never_the_venue():
    r = _read("Bill Evans Trio\n1974-08-12\nOttawa, Ontario\n\n"
              "Lineage: FM broadcast > reel > Nakamichi deck > Hall Sound Studio > FLAC\n")
    assert r["venue"] is None
    assert "FM broadcast" in (r["lineage"] or "")


def test_venue_alone_on_a_line_between_artist_and_place():
    r = _read("Bill Evans Trio\nCamp Fortune\nOttawa, Ontario (Canada)\nAugust 1974\n")
    assert r["venue"] == "Camp Fortune"


def test_fort_abbreviation_is_not_a_connector():
    r = _read("Flea & The Honora Band\nSunday, August 2, 2026\nNewport, RI @ Ft. Adams State Park\n")
    assert r["venue"] == "Fort Adams State Park"


def test_venue_name_before_a_state_is_not_the_city():
    r = _read("Miles Davis Sextet\nDecember 17, 1970\nCellar Door Club, Washington\n")
    assert r["venue"] == "Cellar Door Club"
    assert r["city"] != "Cellar Door Club"


def test_venue_and_city_dashed_on_one_line():
    r = _read("Manzanita Band\n\nGreat American Music Hall--San Francisco, CA\n10/03/1979\n")
    assert r["venue"] == "Great American Music Hall"
    assert r["city"] == "San Francisco"


def test_pipe_separated_artist_and_city():
    r = _read("Warren Haynes | Chicago\n2011-05-06\n")
    assert r["artist"] == "Warren Haynes"
    assert r["city"] == "Chicago"
    assert r["venue"] != "Chicago"


# ── Tracks and sets ──────────────────────────────────────────────────────────

def test_unnumbered_setlist_counts_its_tracks():
    text = ("Art Blakey's Jazz Messengers\n1978-03-04\nStuttgart, Germany\n\n"
            "Moanin'\nBlues March\nA Night in Tunisia\nThe Egyptian\nWhisper Not\n")
    r = _read(text, n_audio=5)
    assert [t["title"] for t in r["tracks"]] == [
        "Moanin'", "Blues March", "A Night in Tunisia", "The Egyptian", "Whisper Not"]


def test_set_headers_label_tracks_all_or_nothing():
    text = ("Pat Metheny Group\n1992-01-28\nTokyo, Japan\n\n"
            "Set 1\n1. First Circle\n2. Last Train Home\nSet 2\n3. Are You Going With Me\n4. Minuano\n")
    r = _read(text)
    assert [t.get("set") for t in r["tracks"]] == ["Set 1", "Set 1", "Set 2", "Set 2"]


def test_printed_seconds_reads_a_trailing_time():
    assert printed_seconds("Dark Star 12:34") == 754
    assert printed_seconds("Carry On (4:59)") == 299
    assert printed_seconds("Intro :45") == 45
    assert printed_seconds("Dark Star") is None


def test_align_durations_confirms_matching_times():
    printed = [300, 240, 180, 360]
    assert align_durations(printed, [301, 239, 181, 359])["status"] == "confirmed"


def test_align_durations_finds_a_shift():
    printed = [300, 240, 180, 360]
    assert align_durations(printed, [30, 301, 239, 181, 359])["status"] == "shifted"


def test_align_durations_rejects_when_nothing_fits():
    assert align_durations([300, 240, 180, 360], [60, 61, 62, 63])["status"] == "rejected"


def test_align_durations_needs_three_printed_times():
    assert align_durations([300, None, None], [300, 200, 100])["status"] == "none"


# ── LibraryIndex ─────────────────────────────────────────────────────────────

def _loo_rows():
    return LibraryIndex.from_rows(
        [(1, "Solo Act"), (2, "Shared Act")], [], [],
        [(1, "Only Hall", None, None, None), (2, "Shared Hall", None, None, None)],
        [], [(10, 1, 1, None), (11, 2, 2, None), (12, 2, 2, None)])


def test_empty_library_is_empty():
    assert LibraryIndex.empty().is_empty


def test_leave_one_out_hides_rows_only_the_item_uses():
    full = _loo_rows()
    assert full.venue_match("Only Hall")
    view = full.without_performance(10)
    assert not view.venue_match("Only Hall")
    assert not view.artist_match("Solo Act")
    assert full.venue_match("Only Hall")          # the full index is untouched


def test_leave_one_out_keeps_rows_another_performance_uses():
    view = _loo_rows().without_performance(11)
    assert view.venue_match("Shared Hall")
    assert view.artist_match("Shared Act")


# ── current_library cache ────────────────────────────────────────────────────

def test_current_library_rebuilds_on_musician_membership_and_event_edits(app):
    from app.extensions import db
    from app.models.artist import Artist
    from app.models.musician import Musician, Membership
    from app.models.venue import Venue
    from app.models.event import Event
    from app.utils.reader.library import current_library

    lib = current_library()
    assert lib.musician_match("Bill Evans")
    m = Musician.query.filter_by(name="Bill Evans").first()
    m.name = "William Evans"
    db.session.commit()
    assert current_library().musician_match("William Evans")

    a = Artist(name="Second Act")
    db.session.add(a)
    db.session.flush()
    link = Membership(artist_id=a.id, musician_id=m.id, order=0)
    db.session.add(link)
    db.session.commit()
    first = current_library()
    other = Musician(name="Someone Else")
    db.session.add(other)
    db.session.flush()
    link.musician_id = other.id            # a relink: same counts, same timestamps
    db.session.commit()
    assert current_library() is not first

    v1 = Venue(name="Hall One")
    v2 = Venue(name="Hall Two")
    db.session.add_all([v1, v2])
    db.session.flush()
    ev = Event(name="Some Festival", venue_id=v1.id)
    db.session.add(ev)
    db.session.commit()
    before = current_library()
    ev.venue_id = v2.id
    db.session.commit()
    assert current_library() is not before


# ── info file as the last set carrier ────────────────────────────────────────

def test_info_sets_stamp_all_or_nothing():
    from app.utils.ingest import _stamp_info_sets
    audio = [{"index": i, "set_number": None, "disc_number": None} for i in (1, 2, 3, 4)]
    tracks = [{"set": "Set 1"}, {"set": "Set 1"}, {"set": "Set 2"}, {"set": "Set 2"}]
    assert _stamp_info_sets(audio, tracks) is True
    assert [a["set_number"] for a in audio] == ["Set 1", "Set 1", "Set 2", "Set 2"]

    audio = [{"index": i, "set_number": None, "disc_number": None} for i in (1, 2, 3, 4)]
    assert _stamp_info_sets(audio, tracks[:3] + [{"set": None}]) is False
    assert _stamp_info_sets(audio, tracks[:3]) is False                  # count differs
    assert all(a["set_number"] is None for a in audio)

    audio[0]["disc_number"] = 1                                          # a folder carrier exists
    assert _stamp_info_sets(audio, tracks) is False


# ── artist lines, stages, festival venues ────────────────────────────────────

@pytest.mark.parametrize("line", ["Crosby, Stills, Nash and Young",
                                  "Crosby, Stills, Nash & Young",
                                  "Tony Rice, Jerry Douglas, Byron House & John Cowan"])
def test_artist_line_keeps_its_full_text(line):
    r = _read(line + "\nDetroit, MI\n12-14-69\n")
    assert r["artist"] == line


def test_stage_comes_from_the_header_not_the_notes():
    text = ("Abigail Washburn & Friends\nGrey Fox Bluegrass Festival\nAncramdale, NY\nJuly 15, 2006\n\n"
            "Source for Main Stage Set: DAUD Master: Schoeps MK41 > V3 > Casio DA-7\n")
    assert _read(text)["stage"] is None


def test_library_festival_venue_row_is_event_evidence():
    lib = LibraryIndex.from_dicts(venues=["Rockygrass Bluegrass Festival", "Merlefest"])
    r = _read("Rockygrass Late Night Jam\n2001-07-29\nRockygrass Bluegrass Festival\nLyons, CO\n", library=lib)
    assert r["venue"] is None
    assert r["event"] == "Rockygrass Bluegrass Festival"


def test_artist_named_mr_sun_keeps_its_name():
    assert _read("Mr. Sun  2023-04-20\nKuumbwa\nSanta Cruz CA\n")["artist"] == "Mr. Sun"


@pytest.mark.parametrize("name", ["Strawberry Park Bluegrass Festival", "Temple University Music Festival",
                                  "House and Barn Mountain Bluegrass Festival",
                                  "Frontier Ranch Bluegrass Festival"])
def test_festival_with_a_venue_word_is_still_an_event(name):
    r = _read(f"Some Band\n2001-06-02\n{name}\nPreston, CT\n")
    assert r["event"] == name
    assert r["venue"] is None


@pytest.mark.parametrize("name", ["Royal Festival Hall", "Festhalle Frankfurt", "Festspielhaus Baden-Baden"])
def test_festival_named_halls_stay_venues(name):
    r = _read(f"Some Band\n2001-06-02\n{name}\nSomewhere, Germany\n")
    assert r["venue"] == name
    assert r["event"] is None


def test_town_park_stage_is_the_venue():
    r = _read("Chris Thile\n06-18-2026\nTelluride Bluegrass Festival\nTown Park Stage\nTelluride, CO\n")
    assert r["event"] == "Telluride Bluegrass Festival"
    assert r["venue"] == "Town Park"
    assert r["stage"] is None
