"""
2026-10-06 (Ryan): two shapes the Queue got wrong.

- Disc folders named after the show with the disc token at the end
  ("jdb1999-05-27d1.shnf", "...d2.shnf") are one show, not two. The token is
  trusted only when two or more siblings share the stem.
- A numbered track line with no title ("02.") is still a track, so the list
  lines up with the audio files by position.
"""
from app.utils import ingest


def test_sibling_disc_folders_read_as_discs():
    names = ["jdb1999-05-27d1.shnf", "jdb1999-05-27d2.shnf"]
    assert ingest._sibling_discs(names) == {names[0]: 1, names[1]: 2}
    assert ingest._parse_set_dir_among(names[1], names) == ("Disc 2", 2, "disc")


def test_a_lone_name_ending_in_a_digit_is_not_a_disc():
    assert ingest._sibling_discs(["jdb1999-05-27d1.shnf"]) == {}
    assert ingest._sibling_discs(["gd77-05-07", "gd77-05-08"]) == {}
    assert ingest._sibling_discs(["Show A d1", "Show B d2"]) == {}


def test_blank_numbered_tracks_are_kept_in_order():
    text = ("Jerry Douglas Band\n2003-07-18\nAncramdale, NY\n\nDISC ONE\n\n"
            "01. < intro >\n02. \n03. \n04. < fade out >\n\nDISC TWO\n\n01. < fade in >\n02. \n")
    tracks = ingest.parse_info_file(None, text=text)["tracks"]
    assert [t["number"] for t in tracks] == [1, 2, 3, 4, 5, 6]
    assert [t["title"] for t in tracks][1:3] == ["", ""]
