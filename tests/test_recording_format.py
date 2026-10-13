"""The recording payload carries its format, from the track files (2026-10-13)."""
from types import SimpleNamespace as NS

from app.utils.bulk_ingest import recording_format


def test_recording_format():
    rec = NS(tracks=[NS(file_path="a/01.mp3"), NS(file_path="a/02.MP3")])
    assert recording_format(rec) == "MP3"
    rec.tracks.append(NS(file_path="a/03.flac"))
    assert recording_format(rec) == "FLAC, MP3"
    assert recording_format(NS(tracks=[])) is None
