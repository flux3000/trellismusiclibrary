"""A non-audio file the OS will not move must not fail the import, and folder cruft
is not carried into the library (2026-10-05: Permission denied on a Windows folder icon)."""
import os
import shutil

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
    real = shutil.move

    def move(a, b):
        if a.endswith("X-Back.png"):
            raise PermissionError(13, "Permission denied", a)
        return real(a, b)
    monkeypatch.setattr(ing.shutil, "move", move)
    rel = ing.move_to_library(str(src), str(lib), "Ahmad Jamal", "show", flatten=False)
    assert (lib / rel / "Set 1" / "01.flac").exists()
    assert (src / "X-Back.png").exists()          # never deleted


def test_an_unmovable_audio_file_still_fails(tmp_path, monkeypatch):
    src, lib = _src(tmp_path)
    real = shutil.move

    def move(a, b):
        if a.endswith(".flac"):
            raise PermissionError(13, "Permission denied", a)
        return real(a, b)
    monkeypatch.setattr(ing.shutil, "move", move)
    with pytest.raises(PermissionError):
        ing.move_to_library(str(src), str(lib), "Ahmad Jamal", "show", flatten=False)
    assert (src / "info.txt").exists() and (src / "Set 1" / "01.flac").exists()   # nothing split
