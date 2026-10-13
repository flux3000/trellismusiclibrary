"""find_incomplete_release(): a queued Album folder that holds part of a longer
official release (2026-10-13; the Herbie Mann track split out of a Various
Artists box set). _get() is faked; enabled()/tripped() are forced on because
MusicBrainz is off under TESTING."""
from pathlib import Path

import pytest

from app.utils import musicbrainz as mb

ALBUM = "What It Is! Funky Soul and Rare Grooves"


def _search(*editions):
    return {"releases": [
        {"id": mbid, "title": ALBUM, "score": 100, "track-count": n,
         "release-group": {"id": group, "primary-type": "Album"},
         "artist-credit": [{"name": "Various Artists"}]}
        for mbid, n, group in editions]}


def _release(mbid, tracks):
    """tracks: [(title, artist)], one medium per 10 tracks."""
    media = [{"tracks": [{"title": t, "artist-credit": [{"name": a}]}
                         for t, a in tracks[i:i + 10]]}
             for i in range(0, len(tracks), 10)]
    return {"id": mbid, "title": ALBUM,
            "artist-credit": [{"name": "Various Artists", "joinphrase": ""}], "media": media}


BOX = [("Cajun Moon", "Herbie Mann")] + [(f"Song {i}", f"Act {i}") for i in range(2, 32)]


@pytest.fixture
def online(monkeypatch):
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "tripped", lambda: False)
    calls = []

    def use(responses):
        def _fake(path, params):
            calls.append(path)
            return responses.get(path)
        monkeypatch.setattr(mb, "_get", _fake)
        return calls
    return use


def test_single_track_from_box_set_is_flagged(online):
    online({"release/": _search(("r1", 31, "g1")), "release/r1": _release("r1", BOX)})
    found = mb.find_incomplete_release("Herbie Mann", ALBUM, ["Cajun Moon"])
    assert found["folder_tracks"] == 1 and found["release_tracks"] == 31
    assert found["mbid"] == "r1" and found["artist_credit"] == "Various Artists"


def test_edition_as_short_as_the_folder_means_complete(online):
    # A one-track edition in the same group: the folder may be that edition.
    calls = online({"release/": _search(("r1", 31, "g1"), ("r2", 1, "g1"))})
    assert mb.find_incomplete_release("Herbie Mann", ALBUM, ["Cajun Moon"]) is None
    assert calls == ["release/"]


def test_track_not_on_the_release_is_not_flagged(online):
    online({"release/": _search(("r1", 31, "g1")), "release/r1": _release("r1", BOX)})
    assert mb.find_incomplete_release("Herbie Mann", ALBUM, ["Memphis Underground"]) is None


def test_artist_not_credited_is_not_flagged(online):
    online({"release/": _search(("r1", 31, "g1")), "release/r1": _release("r1", BOX)})
    assert mb.find_incomplete_release("Someone Else", ALBUM, ["Cajun Moon"]) is None


def test_different_title_is_not_considered(online):
    calls = online({"release/": _search(("r1", 31, "g1"))})
    assert mb.find_incomplete_release("Herbie Mann", "What It Is", ["Cajun Moon"]) is None
    assert calls == ["release/"]


def test_placeholder_titles_never_call_out(online):
    calls = online({})
    assert mb.find_incomplete_release("Herbie Mann", ALBUM, ["Track 1"]) is None
    assert calls == []


def test_off_under_testing_makes_no_call(app, monkeypatch):
    monkeypatch.setattr(mb, "_get", lambda *a: pytest.fail("network call"))
    assert mb.find_incomplete_release("Herbie Mann", ALBUM, ["Cajun Moon"]) is None


def test_reason_has_a_label_on_the_row_and_the_card():
    root = Path(__file__).resolve().parent.parent
    assert "incomplete_release:  'Incomplete release'" in (root / "app/static/js/app.js").read_text()
    assert '"incomplete_release"' in (root / "app/api/quality.py").read_text()
    assert "find_incomplete_release" in (root / "app/api/ingest.py").read_text()


# ── Offline first check: TRACKTOTAL against the files present ────────────────

from app.utils.resolve import tag_track_shortfall


def _scan(*raws, discs=None):
    files = [{"filename": f"{i}.flac", "rel_path": f"{i}.flac",
              "disc_number": (discs or [None] * len(raws))[i]} for i in range(len(raws))]
    tracks = [{"filename": f"{i}.flac", "rel_path": f"{i}.flac", "raw": r}
              for i, r in enumerate(raws)]
    return {"audio_files": files, "suggestions": {"from_tags": {"tracks": tracks}}}


def test_two_files_tagged_of_24_is_short():
    s = _scan({"tracknumber": "1", "tracktotal": "24"}, {"tracknumber": "2", "tracktotal": "24"})
    assert tag_track_shortfall(s) == {"have": 2, "expected": 24, "unit": "tracks"}


def test_total_inside_tracknumber_counts():
    assert tag_track_shortfall(_scan({"tracknumber": "1/12"}))["expected"] == 12


def test_complete_and_untagged_folders_are_not_flagged():
    assert tag_track_shortfall(_scan({"tracktotal": "2"}, {"tracktotal": "2"})) is None
    assert tag_track_shortfall(_scan({"title": "a"}, {"title": "b"})) is None


def test_more_files_than_total_is_not_flagged():
    # an untagged two-disc set: 3 files, each disc's total is 2
    assert tag_track_shortfall(_scan(*[{"tracktotal": "2"}] * 3)) is None


def test_totals_are_per_disc():
    full = [{"discnumber": "1", "tracktotal": "2"}] * 2 + [{"discnumber": "2", "tracktotal": "2"}] * 2
    assert tag_track_shortfall(_scan(*full)) is None
    assert tag_track_shortfall(_scan(*full[:3]))["expected"] == 4


def test_missing_disc_is_short():
    s = _scan({"discnumber": "1/2", "tracktotal": "1"})
    assert tag_track_shortfall(s) == {"have": 1, "expected": 2, "unit": "discs"}


def test_folder_disc_carrier_groups_when_tags_have_no_disc():
    s = _scan({"tracktotal": "1"}, {"tracktotal": "1"}, discs=[1, 2])
    assert tag_track_shortfall(s) is None


def test_two_artist_billing_matches_a_split_credit(online):
    rel = _release("r1", [("Killer Bees on Caffeine", "Béla Fleck")] + BOX[1:])
    rel["artist-credit"] = [{"name": "Béla Fleck", "joinphrase": ", "}, {"name": "Tony Trischka"}]
    online({"release/": _search(("r1", 31, "g1")), "release/r1": rel})
    found = mb.find_incomplete_release("Bela Fleck and Tony Trischka", ALBUM, ["Killer Bees on Caffeine"])
    assert found and found["release_tracks"] == 31


# ── Type: Album, else dated (Live), else Unknown; folder name settles a split ALBUM ──

from app.utils.resolve import classify_kind, verdict, resolve  # noqa: E402


def _kind_scan(albums, folder="Bela Fleck and Tony Trischka - Solo Banjo Works"):
    tracks = [{"filename": f"{i}.mp3", "rel_path": f"{i}.mp3", "raw": {"album": a, "title": f"T{i}"}}
              for i, a in enumerate(albums)]
    return {"folder_name": folder, "audio_files": [{"filename": t["filename"]} for t in tracks],
            "suggestions": {"from_tags": {"tracks": tracks}, "from_info_file": {}}}


def test_split_album_tags_settled_by_folder_name():
    from app.utils.resolve import _resolve_album_field
    f = _resolve_album_field(_kind_scan(["1992", "1992", "Solo Banjo Works", "Solo Banjo Works"]))
    assert f.value == "Solo Banjo Works" and f.source == "folder" and f.conflict


def test_split_album_tags_without_folder_match_stay_unset():
    from app.utils.resolve import _resolve_album_field
    assert _resolve_album_field(_kind_scan(["A", "B"], folder="Someone - C")).value is None


def test_kind_falls_back_to_live_on_a_date_then_unknown():
    s = _kind_scan(["A", "B"], folder="no album here")
    assert classify_kind(s, None, None, {"year": 1992}) == "live"
    assert classify_kind(s, None, None, None) == "unknown"
    assert classify_kind(s, "Album", None, None) == "studio"


def test_unknown_type_needs_a_person_and_no_date_reasons():
    from app.utils.resolve import Resolved, Field
    r = Resolved(**{k: Field() for k in ("date", "artist", "venue", "event", "stage", "city", "state",
                                         "country", "source", "lineage", "source_tag", "shnid", "album")},
                 kind="unknown", tracks=[], scan={})
    r.artist = Field(value="X", confidence="confident")
    status, reasons = verdict(r)
    assert status == "review" and "needs_type" in reasons
    assert not any(x.startswith("needs_") and x != "needs_type" for x in reasons)
