"""A tag with several values (two ARTIST comments) must not crash the resolver
(2026-10-05: 'list' object has no attribute 'lower' on an import row)."""
from app.utils.resolve import _consistent_tag, _tag_container_values


def _t(**raw):
    return {"raw": raw}


def test_a_multi_valued_tag_reads_as_its_first_value():
    tracks = [_t(artist=["Ahmad Jamal Quartet", "Ahmad Jamal"]), _t(artist=["Ahmad Jamal Quartet", "Ahmad Jamal"])]
    assert _tag_container_values(tracks, "artist") == ["Ahmad Jamal Quartet", "Ahmad Jamal Quartet"]
    assert _consistent_tag(tracks, "artist") == "Ahmad Jamal Quartet"


def test_a_list_of_blanks_counts_as_missing():
    assert _tag_container_values([_t(artist=["", "  "], albumartist="X")], "artist", "albumartist") == ["X"]


def test_single_values_are_unchanged():
    assert _consistent_tag([_t(album="A"), _t(album="A")], "album") == "A"
