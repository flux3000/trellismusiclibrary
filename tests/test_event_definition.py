"""
What an Event is (Ryan, 2026-10-05): a collection of performances comprising a festival or
other single ticketed-or-free event. A tour, a residency or a billing note ("Opened for
The Strokes") is not one. One test, counts_as_event(), serves Atlas events and text alike.
"""
import pathlib

import pytest

from app.utils.ingest import build_scan_payload
from app.utils.reader.features import _atlas_show, counts_as_event
from app.utils.resolve import resolve


@pytest.mark.parametrize("name,kind", [
    ("MerleFest 2019", "festival"),
    ("Newport Folk Festival 1965", "festival"),
    ("IBMA 2018", "festival"),
    ("Detroit Jazz Festival 2013", None),            # untyped, but the name is a festival
    ("Grey Fox Bluegrass Festival", None),
])
def test_festivals_are_events(name, kind):
    assert counts_as_event(name, kind)


@pytest.mark.parametrize("name,kind", [
    ("The Strokes at Glasgow Barrowland", "concert"),   # a single show
    ("Broken Music Tour", "concert"),
    ("Fall Tour 2019", None),
    ("Opened for The Strokes", None),
    ("Opening for Ralph Stanley", None),
    ("supporting Bill Monroe", None),
    ("Summer Festival Tour 2004", "festival"),          # a tour, whatever MusicBrainz calls it
    ("Las Vegas Residency", None),
    ("", "festival"),
])
def test_tours_shows_and_billing_notes_are_not_events(name, kind):
    assert not counts_as_event(name, kind)


class _FakeAtlas:
    def __init__(self, shows):
        self.shows = shows

    def event_place(self, artist, date):
        return self.shows

    def place_keys(self, pid):
        return {"hammerstein ballroom"}


def test_a_same_day_atlas_concert_gives_the_place_but_not_an_event():
    atlas = _FakeAtlas([{"event": "The Strokes at Hammerstein Ballroom", "kind": "concert",
                         "place_id": 1, "exact": True}])
    places, events = _atlas_show(atlas, {"artist": "Kings of Leon", "date": (2003, 11, 21)})
    assert places == {"hammerstein ballroom"} and events == set()


def test_a_same_day_atlas_festival_is_still_an_event():
    atlas = _FakeAtlas([{"event": "MerleFest 1995", "kind": "festival", "place_id": 1, "exact": True}])
    assert _atlas_show(atlas, {"artist": "Doc Watson", "date": (1995, 4, 29)})[1] == {"merlefest 1995"}


def _event_of(tmp_path, head):
    show = tmp_path / "show"
    show.mkdir(parents=True)
    (show / "info.txt").write_text(head + "\n\n01. Song One\n")
    (show / "01.flac").write_bytes(b"")
    return resolve(build_scan_payload(str(show)), library_root=str(tmp_path), placement=None).event.value


@pytest.mark.parametrize("head", [
    "Kings of Leon\nOpened for The Strokes\nHammerstein Ballroom, New York, NY\n2003-11-21",
    "Wilco\nEvent: Fall Tour 2019\nThe Fillmore, San Francisco, CA\n2019-10-12",
    "Phish\nSummer Tour Festival Series '97\nThe Gorge, George, WA\n1997-07-21",
])
def test_text_naming_a_tour_or_an_opener_gives_no_event(tmp_path, head):
    assert _event_of(tmp_path, head) is None


def test_text_naming_a_festival_still_gives_the_event(tmp_path):
    assert _event_of(tmp_path, "Doc Watson\nMerleFest\nWilkes Community College, Wilkesboro, NC\n1995-04-29") == "MerleFest"
