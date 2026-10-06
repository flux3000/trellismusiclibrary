"""
tests/test_parse_info_file.py — regression tests for parse_info_file's
track-listing extraction (app/utils/ingest.py).

Covers two bugs Ryan reported 2026-07-16 from a real CSNY info file:
1. A trailing "Notes:" section with its own numbered lines (e.g. "1 - Noise
   at 2:51 from the guys goofing around.") was being misparsed as tracks,
   clobbering the real tracks 1-4.
2. Multi-disc listings restart numbering at 1 each disc ("*** Disc Two ***"
   then "1. Song"); the raw numbers collided instead of coming out
   sequential across the whole recording.
3. A bare total-running-time line in the header ("1:46:28") was read by
   _TRACK_PATTERN as track 1 titled "46:28", displacing every real track
   number by one (Ryan, 2026-08-30, from a real Gary Burton Quintet info
   file).
4. A trailing timestamp given in parentheses ("Carry On (4:59)") was left
   in the title instead of being recognized as a taper's duration note and
   stripped (Ryan, 2026-08-30).
5. A trailing "(Composer Name)" credit ("Ictus / Syndrome (Carla Bley)") is
   now split off into the track's songwriter field rather than left in the
   title (Ryan, 2026-08-30) — deliberately conservative, so an annotation
   that happens to share the same shape ("(Alternate Take)", "(Live)",
   "(Tuning)") is never mistaken for a composer credit.
"""

import pytest
import tempfile
import os

from app.utils.ingest import parse_info_file


def _parse(text):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        return parse_info_file(path)
    finally:
        os.unlink(path)


CSNY_INFO_FILE = """Crosby, Stills, Nash & Young
Fillmore East, New York, NY
6/6/1970
SBD

*** Disc One (49:01) ***
    1. Suite: Judy Blue Eyes
    2. Blackbird
    3. On The Way Home
    4. Teach Your Children [1]
    5. Tell Me Why
    6. Triad
    7. Guinnevere
    8. Simple Man
*** Disc Two (37:57) ***
    1. King Midas In Reverse [2]
    2. The Loner>Cinnamon Girl>Down By The River
    3. Black Queen
    4. 4 + 20
    5. 49 Bye-Byes/America's Children [3]
    6. Love The One You're With (incomplete/fade-out)
*** Disc Three (73:17) ***
    1. Pre-Road Downs
    2. Long Time Gone
    3. Helplessly Hoping
    4. Ohio
    5. As I Come Of Age [4]
    6. Southern Man
    7. Carry On
    8. Woodstock
    9. Find The Cost Of Freedom [4]

Notes:
 1 - Noise at 2:51 & 2:54 from the guys goofing around.
 2 - Skip at 0:32 during Graham's ramblings.
 3 - Remains of static at 0:05-0:08 & 0:15-0:16
 4 - Sound levels have some low moments.
"""


def test_multidisc_tracklist_renumbers_sequentially():
    result = _parse(CSNY_INFO_FILE)
    tracks = result["tracks"]
    assert [t["number"] for t in tracks] == list(range(1, 24))
    assert len(tracks) == 23


def test_notes_section_excluded_from_tracks():
    result = _parse(CSNY_INFO_FILE)
    titles = [t["title"] for t in result["tracks"]]
    assert not any("goofing around" in t.lower() for t in titles)
    assert not any("ramblings" in t.lower() for t in titles)
    assert not any("static" in t.lower() for t in titles)
    assert not any("low moments" in t.lower() for t in titles)


def test_first_and_last_disc_titles_preserved():
    result = _parse(CSNY_INFO_FILE)
    tracks = result["tracks"]
    assert tracks[0]["title"] == "Suite: Judy Blue Eyes"
    assert tracks[-1]["title"] == "Find the Cost of Freedom [4]"
    # Track 9 is the first track of Disc Two — this is exactly the number
    # that used to collide with Disc One's own track 9 (there is none here,
    # since Disc One only has 8, but the offset math is what matters).
    assert tracks[8]["title"] == "King Midas in Reverse [2]"


GARY_BURTON_INFO_FILE = """Gary Burton Quintet
Studio 104, la Maison de la Radio, Paris, France
December 13, 1975
1:46:28

http://www.garyburton.com/

FM > Edirol R-09 (WAV) > WaveLab > FLAC (level 8, sector-align)
Broadcasts : Les L\u00e9gendes du Jazz, France Musique, may 26 & 27, 2018
Cosmikd

Gary Burton (vibes)
Mick Goodrick (guitar)
Pat Metheny (guitar)
Steve Swallow (bass)
Bob Moses (drums)

01 Ictus / Syndrome (Carla Bley)  6:45
02 Vashkar (Carla Bley)  5:50
03 Desert Air (Chick Corea)  6:42
04 The Colors of Chlo\u00eb (Eberhard Weber)  7:11
05 Drum Solo (Bob Moses)  5:48
"""


def test_header_total_time_line_is_not_read_as_track_one():
    # "1:46:28" on its own line is the stated total running time, not a
    # track — it must not become track 1 titled "46:28", and it must not
    # displace the real tracks by one.
    result = _parse(GARY_BURTON_INFO_FILE)
    tracks = result["tracks"]
    assert [t["number"] for t in tracks] == [1, 2, 3, 4, 5]
    # The composer credit in parens is a SEPARATE feature (see
    # test_trailing_songwriter_credit_split_from_title below) and splits out
    # on its own — assert against the title it actually leaves behind.
    assert tracks[0]["title"] == "Ictus / Syndrome"
    assert tracks[0]["songwriter"] == "Carla Bley"
    assert not any("46:28" in t["title"] for t in tracks)


def test_trailing_parenthetical_timestamp_stripped_from_title():
    # Taper habit: "Carry On (4:59)". The paren'd time is not part of the
    # title and should be stripped — but a genuine non-timestamp annotation
    # in parens, like "(Alternate Take)", must survive untouched. (A genuine
    # SONGWRITER credit in parens is a separate feature with its own test,
    # test_trailing_songwriter_credit_split_from_title — kept out of this
    # fixture so the two features' tests don't couple.)
    text = """Some Band
Some Venue
1/1/2000

1. Carry On (4:59)
2. Dark Star (Alternate Take)
3. Long Time Gone (1:04:59)
"""
    result = _parse(text)
    titles = [t["title"] for t in result["tracks"]]
    # "on" is one of title_case()'s kept-lowercase minor words (same AP-style
    # rule that keeps "of"/"in" lowercase elsewhere) — "Carry on" is the
    # app's correct, existing casing, not a casualty of this fix.
    assert titles == ["Carry on", "Dark Star (Alternate Take)", "Long Time Gone"]


def test_trailing_songwriter_credit_split_from_title():
    text = """Some Band
Some Venue
1/1/2000

1. Ictus / Syndrome (Carla Bley)
2. Carry On (Stephen Stills)
3. St. Stephen (Weir)
"""
    result = _parse(text)
    tracks = result["tracks"]
    assert [t["title"] for t in tracks] == \
        ["Ictus / Syndrome", "Carry on", "St. Stephen"]
    assert [t["songwriter"] for t in tracks] == \
        ["Carla Bley", "Stephen Stills", "Weir"]


def test_trailing_paren_annotations_are_not_mistaken_for_a_songwriter():
    # Same bracket shape as a composer credit, but none of these are one:
    # a recording annotation, a segment label crediting who did it, and a
    # multi-writer credit this feature deliberately leaves alone.
    text = """Some Band
Some Venue
1/1/2000

1. Dark Star (Live)
2. Dark Star (Alternate Take)
3. Tuning (Bobby)
4. Cassidy (Bob Weir/John Barlow)
"""
    result = _parse(text)
    tracks = result["tracks"]
    assert [t["songwriter"] for t in tracks] == [None, None, None, None]
    # And the parenthetical stays put on the title when it's not extracted.
    assert tracks[2]["title"] == "Tuning (Bobby)"


def test_single_disc_no_notes_still_works():
    text = """Some Band
Some Venue
1/1/2000

1. First Song
2. Second Song
3. Third Song
"""
    result = _parse(text)
    tracks = result["tracks"]
    assert [t["number"] for t in tracks] == [1, 2, 3]
    assert [t["title"] for t in tracks] == ["First Song", "Second Song", "Third Song"]


# ── invisible characters in the info text (Ryan, 2026-10-05) ─────────────────

_Z = "﻿"
_SETLIST = (
    "Setlist:\n01 Green Sugar (22:27)\n" + _Z * 3 + "02 Dancing Blue (8:21)\n"
    "03 " + _Z * 3 + "Zo no Senaka (6:50)\n04 " + _Z * 3 + "Smoke and Mirrors (9:50)\n"
    "05 " + _Z * 3 + "Entrance (3:39)\n06 " + _Z * 3 + "Dripping Sun (8:31)\n"
    "07 " + _Z * 3 + "Orange Peel (6:45)\n08 " + _Z * 3 + "Yayoi, lyayoi (7:52)\n"
    "09 " + _Z * 3 + "Kodama (5:20)\n" + _Z * 4 + "10 Streets of Calcutta - " + _Z * 3 + "Gatherings (17:30)\n"
    "11 Monaka (9:37)\n")


def test_a_setlist_wrapped_in_feff_reads_as_eleven_tracks():
    from app.utils.ingest import parse_info_file
    r = parse_info_file(None, text=_SETLIST)
    assert [t["number"] for t in r["tracks"]] == list(range(1, 12))
    assert [t["title"] for t in r["tracks"]] == [
        "Green Sugar", "Dancing Blue", "Zo No Senaka", "Smoke and Mirrors", "Entrance", "Dripping Sun",
        "Orange Peel", "Yayoi, Lyayoi", "Kodama", "Streets of Calcutta - Gatherings", "Monaka"]


def test_clean_info_text_strips_every_invisible_character_everywhere():
    from app.utils.ingest import clean_info_text
    assert clean_info_text("﻿A​B‌C‍D⁠E﻿") == "ABCDE"
    assert clean_info_text(None) == ""


def test_the_same_setlist_without_the_invisible_characters_reads_the_same():
    from app.utils.ingest import parse_info_file
    assert parse_info_file(None, text=_SETLIST)["tracks"] == parse_info_file(None, text=_SETLIST.replace(_Z, ""))["tracks"]


def test_a_setlist_heading_starts_the_tracks_but_a_bare_number_line_alone_does_not():
    from app.utils.ingest import parse_info_file
    assert len(parse_info_file(None, text="Setlist:\n01 One\n02 Two\n")["tracks"]) == 2
    assert parse_info_file(None, text="1999 Tour\n")["tracks"] == []


@pytest.mark.parametrize("head", ["Equipment:", "Lineage:", "Thanks:", "Notes:", "Source:"])
def test_a_numbered_list_under_another_heading_is_not_read_as_tracks(head):
    from app.utils.ingest import parse_info_file
    assert parse_info_file(None, text=f"{head}\n1 Neumann KM140\n")["tracks"] == []


def test_setlist_heading_then_other_heading_then_numbers_is_not_a_setlist_flag_leak():
    from app.utils.ingest import parse_info_file
    r = parse_info_file(None, text="Setlist:\n01 One\n02 Two\nEquipment:\n1 Neumann KM140\n")
    assert [t["title"] for t in r["tracks"]][:2] == ["One", "Two"]


# ── parsing is deterministic (the place peeler's cached result used to be edited in place) ──

@pytest.mark.parametrize("text", [
    'Hot Rize 4.3.90\r\nThe Wheeler Opry House -Aspen,CO\r\n"Farewell Colorado"\r\n\r\n-DISC 1-\r\n1. Sandy talks/Blue Night\r\n2. Keep Your Lamps Trimmed and Burning\r\n',
    "Miles Davis Sextet\r\nDecember 17, 1970 \r\nCellar Door Club, Washington\r\n\r\n01. So What\r\n02. Freddie Freeloader\r\n",
    "Manzanita Band\r\n\r\nGreat American Music Hall--San Francisco, CA\r\n10/03/1979\r\n\r\n01. One\r\n02. Two\r\n",
])
def test_parsing_the_same_text_three_times_gives_the_same_result(text):
    from app.utils.ingest import parse_info_file
    a, b, c = (parse_info_file(None, text=text) for _ in range(3))
    assert a == b == c
