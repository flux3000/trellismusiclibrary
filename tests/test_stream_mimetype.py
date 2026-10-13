"""Local streams carry the file's own type: an MP3 is audio/mpeg (2026-10-13)."""
from app.api.stream import _mimetype_of


def test_mimetype_by_extension():
    assert _mimetype_of("/x/01 - Wild as the Wind.mp3") == "audio/mpeg"
    assert _mimetype_of("/x/01.FLAC") == "audio/flac"
