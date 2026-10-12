"""A non-audio file the OS will not move must not fail the import, and folder
cruft is not carried into the library.
"""
import os

import pytest

from app.utils import ingest as ing


def _src(tmp_path):
    src = tmp_path / "dl" / "1988-12-22 Ahmad Jamal Quartet Carlos 1, NYC"
    (src / "Set 1").mkdir(parents=True)
    (src / "Set 1" / "01.flac").write_bytes(b"x")
    (src / "info.txt").write_text("Ahmad Jamal Quartet")
    (src / "Ahmad.ico").write_bytes(b"i")
    (src / "desktop.ini").write_text("[.ShellClassInfo]")
    (src / "X-Back.png").write_bytes(b"p")
    lib = tmp_path / "lib"
    lib.mkdir()
    return src, lib


def test_cruft_stays_out_of_the_library(tmp_path):
    src, lib = _src(tmp_path)
    rel = ing.move_to_library(str(src), str(lib), "Ahmad Jamal", "show", flatten=False)
    dest = lib / rel
    assert (dest / "Set 1" / "01.flac").exists() and (dest / "info.txt").exists()
    assert not (dest / "Ahmad.ico").exists() and not (dest / "desktop.ini").exists()
    assert not src.exists()


def test_an_unmovable_extra_is_left_behind_not_fatal(tmp_path, monkeypatch):
    src, lib = _src(tmp_path)
    real = os.rename

    def move(a, b):
        if str(a).endswith("X-Back.png"):
            raise PermissionError(13, "Permission denied", a)
        return real(a, b)
    monkeypatch.setattr(os, "rename", move)
    rel = ing.move_to_library(str(src), str(lib), "Ahmad Jamal", "show", flatten=False)
    assert (lib / rel / "Set 1" / "01.flac").exists()
    assert (src / "X-Back.png").exists()          # never deleted


def test_an_unmovable_audio_file_still_fails(tmp_path, monkeypatch):
    src, lib = _src(tmp_path)
    real = os.rename

    def move(a, b):
        if str(a).endswith(".flac"):
            raise PermissionError(13, "Permission denied", a)
        return real(a, b)
    monkeypatch.setattr(os, "rename", move)
    with pytest.raises(PermissionError):
        ing.move_to_library(str(src), str(lib), "Ahmad Jamal", "show", flatten=False)
    assert (src / "info.txt").exists() and (src / "Set 1" / "01.flac").exists()   # nothing split


def _cross_volume(monkeypatch, fail_unlink_on=None):
    """Every rename reports a different volume; unlinking `fail_unlink_on` (a source) is refused."""
    import errno
    real_unlink = os.unlink

    def rename(a, b):
        raise OSError(errno.EXDEV, "Invalid cross-device link", a)

    def unlink(path, *a, **k):
        if fail_unlink_on and str(path).endswith(fail_unlink_on) and "/dl/" in str(path):   # the source side only
            raise PermissionError(13, "Permission denied", path)
        return real_unlink(path, *a, **k)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "unlink", unlink)


def test_cross_volume_move_works_by_copy_then_delete(tmp_path, monkeypatch):
    src, lib = _src(tmp_path)
    _cross_volume(monkeypatch)
    rel = ing.move_to_library(str(src), str(lib), "Ahmad Jamal", "show", flatten=False)
    assert (lib / rel / "Set 1" / "01.flac").exists() and not src.exists()


def test_source_unlink_failure_leaves_no_orphan_in_the_library(tmp_path, monkeypatch):
    src, lib = _src(tmp_path)
    _cross_volume(monkeypatch, fail_unlink_on="01.flac")
    with pytest.raises(PermissionError):
        ing.move_to_library(str(src), str(lib), "Ahmad Jamal", "show", flatten=False)
    assert (src / "Set 1" / "01.flac").exists() and (src / "info.txt").exists()     # source intact
    assert not any(p.is_file() for p in lib.rglob("*"))                          # no copy left behind
    assert not (lib / "Ahmad Jamal").exists()                                    # no empty folders either


def test_earlier_files_are_restored_when_a_later_audio_file_fails(tmp_path, monkeypatch):
    src, lib = _src(tmp_path)
    (src / "Set 1" / "02.flac").write_bytes(b"y")
    _cross_volume(monkeypatch, fail_unlink_on="02.flac")
    with pytest.raises(PermissionError):
        ing.move_to_library(str(src), str(lib), "Ahmad Jamal", "show", flatten=False)
    assert (src / "Set 1" / "01.flac").exists() and (src / "Set 1" / "02.flac").exists()
    assert (src / "info.txt").exists()
    assert not any(p.is_file() for p in lib.rglob("*"))
