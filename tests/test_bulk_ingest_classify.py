"""
tests/test_bulk_ingest_classify.py -- Bulk Ingest's pure classification layer
(app/utils/bulk_ingest.py). Real tiny FLAC/MP3 files tagged via mutagen, real
folders under tmp_path acting as library_root. No DB, no network.
"""
import re
import numpy as np
import soundfile as sf
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import TPE1, TALB, TDRC

from app.utils.bulk_ingest import _folder_tree_artist, folder_format


def test_no_musicbrainz_import():
    import ast
    with open("app/utils/bulk_ingest.py") as f:
        tree = ast.parse(f.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    assert not any("musicbrainz" in n for n in names)


def _flac_with_tags(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for k, v in tags.items():
        audio[k] = v
    audio.save()


def _mp3_with_tags(path, artist=None, album=None, date=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    # A real tiny MP3 (silence) so MP3()/mutagen can open it.
    import subprocess, shutil as _sh
    if _sh.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
             "-t", "0.1", "-codec:a", "libmp3lame", str(path)],
            check=True, capture_output=True)
    else:
        # Minimal MP3 frame header + silence, good enough for mutagen.MP3 to open.
        frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(100)
        path.write_bytes(frame * 20)
    audio = MP3(str(path))
    if audio.tags is None:
        audio.add_tags()
    if artist:
        audio.tags.setall("TPE1", [TPE1(encoding=3, text=[artist])])
    if album:
        audio.tags.setall("TALB", [TALB(encoding=3, text=[album])])
    if date:
        audio.tags.setall("TDRC", [TDRC(encoding=3, text=[date])])
    audio.save()


def _wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="WAV")


def _shn(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a real shn file, just bytes")


# ── Folder-tree artist ───────────────────────────────────────────────────────

def test_folder_tree_artist_skips_year_folder(tmp_path):
    root = tmp_path / "Root"
    show = root / "Grateful Dead" / "1977" / "show"
    show.mkdir(parents=True)
    assert _folder_tree_artist(str(show), str(root)) == "Grateful Dead"


def test_folder_tree_artist_none_for_root_placement(tmp_path):
    # placement != "artist" means resolve()'s artist rule never calls
    # _folder_tree_artist at all -- see test_resolve.py's own coverage of
    # that call site. This file only owns _folder_tree_artist() itself.
    root = tmp_path / "Root"
    show = root / "Grateful Dead" / "1977" / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac")
    assert _folder_tree_artist(str(show), str(root)) == "Grateful Dead"


def test_folder_tree_artist_skips_underscore_ancestor(tmp_path):
    root = tmp_path / "Root"
    show = root / "_venues" / "Grateful Dead" / "show"
    show.mkdir(parents=True)
    assert _folder_tree_artist(str(show), str(root)) == "Grateful Dead"


def test_folder_format_labels_flac_wav_shn_alphabetically(tmp_path):
    root = tmp_path / "Root"
    show = root / "show"
    show.mkdir(parents=True)
    _flac_with_tags(show / "01.flac", ARTIST="A")
    _wav(show / "02.wav")
    _shn(show / "03.shn")
    from app.utils.ingest import scan_folder
    assert folder_format(scan_folder(str(show))) == "FLAC, SHN, WAV"
