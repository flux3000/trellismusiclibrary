"""
tests/test_ffmpeg_resolution.py — a packaged app must still find ffmpeg.

An app launched from the Dock does not inherit a terminal's PATH; macOS hands
it a minimal one that excludes /opt/homebrew/bin, which is exactly where
Homebrew puts ffmpeg on Apple Silicon. So `ffmpeg` resolves from a terminal and
not from the app, and every play fails with "Transcoder unavailable" — which
reads as a sharing or network fault rather than a missing program.
"""

import os

import pytest

from app.utils import transcode


@pytest.fixture()
def ctx(app):
    with app.test_request_context():
        yield app


def test_an_explicit_setting_wins(ctx, monkeypatch):
    """Including a wrong one. A typo must fail loudly rather than be silently
    corrected to something that happens to work."""
    ctx.config["FFMPEG_BIN"] = "/nonsense/ffmpeg"
    monkeypatch.setattr(transcode.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    assert transcode.resolve_ffmpeg() == "/nonsense/ffmpeg"


def test_path_is_used_when_nothing_is_configured(ctx, monkeypatch):
    ctx.config["FFMPEG_BIN"] = None
    monkeypatch.setattr(transcode.shutil, "which", lambda _: "/somewhere/ffmpeg")
    assert transcode.resolve_ffmpeg() == "/somewhere/ffmpeg"


def test_homebrew_is_found_when_path_is_empty(ctx, monkeypatch):
    """The actual packaged-app case: PATH has nothing, Homebrew has it."""
    ctx.config["FFMPEG_BIN"] = None
    monkeypatch.setattr(transcode.shutil, "which", lambda _: None)
    monkeypatch.setattr(transcode.os.path, "isfile",
                        lambda p: p == "/opt/homebrew/bin/ffmpeg")
    monkeypatch.setattr(transcode.os, "access", lambda p, m: True)
    assert transcode.resolve_ffmpeg() == "/opt/homebrew/bin/ffmpeg"


def test_intel_mac_location_is_also_searched(ctx, monkeypatch):
    ctx.config["FFMPEG_BIN"] = None
    monkeypatch.setattr(transcode.shutil, "which", lambda _: None)
    monkeypatch.setattr(transcode.os.path, "isfile",
                        lambda p: p == "/usr/local/bin/ffmpeg")
    monkeypatch.setattr(transcode.os, "access", lambda p, m: True)
    assert transcode.resolve_ffmpeg() == "/usr/local/bin/ffmpeg"


def test_a_non_executable_file_is_not_accepted(ctx, monkeypatch):
    """Present but not runnable is not found."""
    ctx.config["FFMPEG_BIN"] = None
    monkeypatch.setattr(transcode.shutil, "which", lambda _: None)
    monkeypatch.setattr(transcode.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(transcode.os, "access", lambda p, m: False)
    assert transcode.resolve_ffmpeg() == "ffmpeg"      # falls through to failure


def test_giving_up_still_returns_something_runnable_to_report(ctx, monkeypatch):
    """When nothing is found we return the bare name so the subprocess call
    raises, and the error names every place we looked."""
    ctx.config["FFMPEG_BIN"] = None
    monkeypatch.setattr(transcode.shutil, "which", lambda _: None)
    monkeypatch.setattr(transcode.os.path, "isfile", lambda p: False)
    assert transcode.resolve_ffmpeg() == "ffmpeg"
    assert "/opt/homebrew/bin/ffmpeg" in ", ".join(transcode._FFMPEG_FALLBACKS)
