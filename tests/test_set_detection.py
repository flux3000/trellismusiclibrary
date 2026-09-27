"""
tests/test_set_detection.py — disc vs set carriers (spec section 1.4 / 5).

Disc carriers (cd, disc, disk, d, vol, volume, part, tape, show subdirs, and
cd1t01/d1t01 filenames) fill Track.disc_number/disc_track_number. Set
carriers (Set N subdirs, the literal Encore subdir, and s1t01 filenames) fill
Track.set_number. The two families never bridge, and `index`/track_number
stay continuous either way.
"""

from app.utils.ingest import scan_folder


def test_set_subdirs_fill_set_number_only(tmp_path):
    root = tmp_path / "sets_src"; root.mkdir()
    s1 = root / "Set 1"; s1.mkdir()
    s2 = root / "Set 2"; s2.mkdir()
    (s1 / "01.flac").write_bytes(b"x")
    (s1 / "02.flac").write_bytes(b"x")
    (s2 / "01.flac").write_bytes(b"x")

    result = scan_folder(str(root))

    assert result["sets_detected"] is True
    assert [f["index"] for f in result["audio_files"]] == [1, 2, 3]
    assert [f["set_number"] for f in result["audio_files"]] == ["Set 1", "Set 1", "Set 2"]
    assert all(f["disc_number"] is None for f in result["audio_files"])
    assert all(f["disc_track_number"] is None for f in result["audio_files"])


def test_encore_subdir_fills_set_number_only(tmp_path):
    root = tmp_path / "encore_src"; root.mkdir()
    s1 = root / "Set 1"; s1.mkdir()
    enc = root / "Encore"; enc.mkdir()
    (s1 / "01.flac").write_bytes(b"x")
    (s1 / "02.flac").write_bytes(b"x")
    (enc / "01.flac").write_bytes(b"x")

    result = scan_folder(str(root))

    assert result["sets_detected"] is True
    assert [f["index"] for f in result["audio_files"]] == [1, 2, 3]
    assert [f["set_number"] for f in result["audio_files"]] == ["Set 1", "Set 1", "Encore"]
    assert all(f["disc_number"] is None for f in result["audio_files"])


def test_filename_set_prefix_fills_set_number_only(tmp_path):
    # _FILENAME_SET_RE is anchored at the start of the basename (a mid-name
    # prefix like "gd1988-05-01s1t01.flac" is a known, deliberate limitation
    # — see its docstring), so the convention is exercised at the position
    # it actually detects.
    root = tmp_path / "flat_sets"; root.mkdir()
    (root / "s1t01.flac").write_bytes(b"x")
    (root / "s1t02.flac").write_bytes(b"x")
    (root / "s2t01.flac").write_bytes(b"x")

    result = scan_folder(str(root))

    assert result["sets_detected"] is True
    assert [f["index"] for f in result["audio_files"]] == [1, 2, 3]
    assert [f["set_number"] for f in result["audio_files"]] == ["Set 1", "Set 1", "Set 2"]
    assert all(f["disc_number"] is None for f in result["audio_files"])
    assert all(f["disc_track_number"] is None for f in result["audio_files"])


def test_cd_subdir_source_leaves_set_number_none(tmp_path):
    root = tmp_path / "cd_src"; root.mkdir()
    cd1 = root / "CD1"; cd1.mkdir()
    cd2 = root / "CD2"; cd2.mkdir()
    (cd1 / "01.flac").write_bytes(b"x")
    (cd2 / "01.flac").write_bytes(b"x")

    result = scan_folder(str(root))

    assert result["sets_detected"] is True
    assert all(f["set_number"] is None for f in result["audio_files"])
    assert [f["disc_number"] for f in result["audio_files"]] == [1, 2]
