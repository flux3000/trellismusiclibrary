"""
tests/test_recording_images.py — recording-level artwork (Studio Records
spec v1, chunk 4): app/utils/recording_images.py and the new ingest step
(_do_confirm step 13) that calls it, for both the interactive and bulk doors.

Fixtures (design spec build plan chunk 4):
  (a) a folder image cover.jpg plus back.png
  (b) a FLAC with an embedded front PICTURE and no folder image
  (c) an MP3 with an embedded APIC and no folder image
  (d) a CD1/CD2 folder with folder.jpg only inside CD1
  (e) no images at all

Every fixture is ingested through the real _do_confirm -- (a) through both
bulk=False and bulk=True. Each source folder is placed INSIDE LIBRARY_ROOT
(an "in-root source", Bulk Ingest spec section 1.1): move_to_library() never
moves, renames or flattens a source that already sits under LIBRARY_ROOT --
it is cataloged exactly where it is -- so a real end-to-end _do_confirm call
can be hashed before and after without the unrelated fact that an
OUTSIDE-root source gets physically relocated (this codebase has no
copy-only path) getting in the way of proving that the artwork step itself
only reads the folder, never writes into it.
"""

import base64
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC

from app.extensions import db as _db
from app.models.recording import Recording
from app.models.recording_image import RecordingImage
from app.models.user import User


# ── Audio fixtures ───────────────────────────────────────────────────────────

def _silent_flac(path, picture_bytes=None, picture_mime="image/jpeg", picture_type=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    if picture_bytes is not None:
        audio = FLAC(str(path))
        pic = Picture()
        pic.data = picture_bytes
        pic.type = picture_type
        pic.mime = picture_mime
        audio.add_picture(pic)
        audio.save()


# A minimal, real (ffmpeg-encoded) silent MP3, base64-encoded for machines
# without ffmpeg -- same fixture tests/test_flac_tag_taxonomy.py uses.
_TINY_MP3_B64 = (
    "SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4Ljc2LjEwMAAAAAAAAAAAAAAA//tAwAAAAAAA"
    "AAAAAAAAAAAAAAAAASW5mbwAAAA8AAAADAAAB7wCTk5OTk5OTk5OTk5OTk5OTk5OTk5OTk5"
    "OTk5OTk5OTk5PKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr////////////"
    "////////////////////////////////////8AAAAATGF2YzU4LjEzAAAAAAAAAAAAAAAA"
    "JAKjAAAAAAAAAe8wbCjYAAAAAAD/+xDEAAPAAAGkAAAAIAAANIAAAARMQU1FMy4xMDBVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVf/7EsQpg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/7EMRTg8AAAaQAAAAgAAA0gAAABFVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
)


def _silent_mp3(path, apic_bytes=None, apic_mime="image/jpeg", apic_type=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
             "-t", "0.05", "-codec:a", "libmp3lame", "-b:a", "32k", str(path)],
            check=True, capture_output=True,
        )
    else:
        path.write_bytes(base64.b64decode(_TINY_MP3_B64))
    if apic_bytes is not None:
        from mutagen.mp3 import MP3
        audio = MP3(str(path))
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(APIC(encoding=3, mime=apic_mime, type=apic_type,
                             desc="cover", data=apic_bytes))
        audio.save()


def _folder_sha256(folder):
    """Deterministic hash of every file's relative path + content, so the
    'source folder untouched' assertion catches a moved, renamed, or
    content-changed file, not just a missing one."""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, folder)
            h.update(rel.encode())
            h.update(Path(full).read_bytes())
    return h.hexdigest()


def _confirm_payload(src, artist_name, tracks):
    return {
        "source_folder_path": str(src),
        "artist_name": artist_name,
        "start_year": 1978, "start_month": 6, "start_day": 1,
        "source": "SBD",
        "tracks": tracks,
        "skip_analysis": True,
    }


_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"jpegdata" * 8
_PNG_BYTES  = b"\x89PNG\r\n\x1a\n" + b"pngdata" * 8


# ── (a) folder image: cover.jpg + back.png, both doors ──────────────────────

@pytest.mark.parametrize("bulk", [False, True])
def test_folder_images_ingested_and_cover_is_primary(app, db, tmp_path, bulk):
    from app.api.ingest import _do_confirm

    lib = tmp_path / f"lib_folder_{bulk}"
    lib.mkdir()
    # In-root source (Bulk Ingest spec 1.1): sits inside LIBRARY_ROOT, so
    # move_to_library() catalogs it in place instead of relocating it.
    src = lib / f"Folder Image Act {bulk}" / "Show1"
    src.mkdir(parents=True)
    _silent_flac(src / "t01.flac")
    (src / "cover.jpg").write_bytes(_JPEG_BYTES)
    (src / "back.png").write_bytes(_PNG_BYTES)

    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["DATA_DIR"] = str(tmp_path / f"data_folder_{bulk}")
    uid = db.session.query(User).first().id

    before_hash = _folder_sha256(src)

    payload = _confirm_payload(src, f"Folder Image Act {bulk}",
                                [{"track_number": 1, "title": "One",
                                  "duration": 100, "filename": "t01.flac"}])
    result = _do_confirm(payload, uid)
    assert result["image_errors"] == []

    after_hash = _folder_sha256(src)
    assert after_hash == before_hash, "source folder must be byte-identical after ingest"

    rec = db.session.get(Recording, result["recording_id"])
    images = db.session.query(RecordingImage).filter_by(recording_id=rec.id).all()
    assert len(images) == 2
    origins = {i.origin for i in images}
    assert origins == {"folder"}
    refs = {i.source_ref for i in images}
    assert refs == {"cover.jpg", "back.png"}

    primaries = [i for i in images if i.is_primary]
    assert len(primaries) == 1
    assert primaries[0].source_ref == "cover.jpg"

    for img in images:
        on_disk = Path(app.config["DATA_DIR"]) / "images" / "recordings" / str(rec.id) / img.filename
        assert on_disk.exists()

    # image_dir() for a recording is keyed by id, never under LIBRARY_ROOT.
    from app.utils.entity_images import image_dir
    with app.app_context():
        d = image_dir("recordings", str(rec.id))
    assert str(lib) not in str(d)
    assert d == Path(app.config["DATA_DIR"]) / "images" / "recordings" / str(rec.id)


def test_reingest_of_same_folder_adds_no_rows(app, db, tmp_path):
    """Calling ingest_recording_images twice against the same recording (the
    dedupe key is source_ref, keyed off the recording, so a re-run --
    whether from a real re-ingest path or a retry -- must not duplicate)."""
    from app.api.ingest import _do_confirm
    from app.utils.recording_images import ingest_recording_images

    lib = tmp_path / "lib_reingest"
    src = lib / "Reingest Act" / "Show1"
    src.mkdir(parents=True)
    _silent_flac(src / "t01.flac")
    (src / "cover.jpg").write_bytes(_JPEG_BYTES)

    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["DATA_DIR"] = str(tmp_path / "data_reingest")
    uid = db.session.query(User).first().id

    payload = _confirm_payload(src, "Reingest Act",
                                [{"track_number": 1, "title": "One",
                                  "duration": 100, "filename": "t01.flac"}])
    result = _do_confirm(payload, uid)
    rec = db.session.get(Recording, result["recording_id"])

    n_before = db.session.query(RecordingImage).filter_by(recording_id=rec.id).count()
    assert n_before == 1

    before_hash = _folder_sha256(src)
    n_added = ingest_recording_images(rec, app.config["LIBRARY_ROOT"])
    db.session.commit()
    assert n_added == 0
    assert _folder_sha256(src) == before_hash

    n_after = db.session.query(RecordingImage).filter_by(recording_id=rec.id).count()
    assert n_after == n_before


# ── (b) FLAC embedded front picture, no folder image ────────────────────────

def test_flac_embedded_front_picture(app, db, tmp_path):
    from app.api.ingest import _do_confirm

    lib = tmp_path / "lib_flac_embedded"
    src = lib / "FLAC Embedded Act" / "Show1"
    src.mkdir(parents=True)
    _silent_flac(src / "t01.flac", picture_bytes=_JPEG_BYTES,
                 picture_mime="image/jpeg", picture_type=3)

    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["DATA_DIR"] = str(tmp_path / "data_flac_embedded")
    uid = db.session.query(User).first().id

    before_hash = _folder_sha256(src)

    payload = _confirm_payload(src, "FLAC Embedded Act",
                                [{"track_number": 1, "title": "One",
                                  "duration": 100, "filename": "t01.flac"}])
    result = _do_confirm(payload, uid)
    assert result["image_errors"] == []
    assert _folder_sha256(src) == before_hash

    rec = db.session.get(Recording, result["recording_id"])
    images = db.session.query(RecordingImage).filter_by(recording_id=rec.id).all()
    assert len(images) == 1
    img = images[0]
    assert img.origin == "embedded"
    assert img.source_ref.startswith("embedded:")
    assert img.is_primary is True
    on_disk = Path(app.config["DATA_DIR"]) / "images" / "recordings" / str(rec.id) / img.filename
    assert on_disk.exists()
    assert on_disk.read_bytes() == _JPEG_BYTES


# ── (c) MP3 embedded APIC, no folder image ──────────────────────────────────

def test_mp3_embedded_apic(app, db, tmp_path):
    from app.api.ingest import _do_confirm

    lib = tmp_path / "lib_mp3_embedded"
    src = lib / "MP3 Embedded Act" / "Show1"
    src.mkdir(parents=True)
    _silent_mp3(src / "t01.mp3", apic_bytes=_JPEG_BYTES,
                apic_mime="image/jpeg", apic_type=3)

    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["DATA_DIR"] = str(tmp_path / "data_mp3_embedded")
    uid = db.session.query(User).first().id

    before_hash = _folder_sha256(src)

    payload = _confirm_payload(src, "MP3 Embedded Act",
                                [{"track_number": 1, "title": "One",
                                  "duration": 100, "filename": "t01.mp3"}])
    result = _do_confirm(payload, uid)
    assert result["image_errors"] == []
    assert _folder_sha256(src) == before_hash

    rec = db.session.get(Recording, result["recording_id"])
    images = db.session.query(RecordingImage).filter_by(recording_id=rec.id).all()
    assert len(images) == 1
    img = images[0]
    assert img.origin == "embedded"
    assert img.is_primary is True
    on_disk = Path(app.config["DATA_DIR"]) / "images" / "recordings" / str(rec.id) / img.filename
    assert on_disk.read_bytes() == _JPEG_BYTES


# ── (d) CD1/CD2 folder, folder.jpg only inside CD1 ──────────────────────────

def test_multidisc_folder_image_only_in_first_disc(app, db, tmp_path):
    from app.api.ingest import _do_confirm

    lib = tmp_path / "lib_multidisc"
    src = lib / "Multidisc Act" / "Show1"
    _silent_flac(src / "CD1" / "t01.flac")
    _silent_flac(src / "CD2" / "t02.flac")
    (src / "CD1" / "folder.jpg").write_bytes(_JPEG_BYTES)

    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["DATA_DIR"] = str(tmp_path / "data_multidisc")
    uid = db.session.query(User).first().id

    before_hash = _folder_sha256(src)

    payload = _confirm_payload(src, "Multidisc Act", [
        {"track_number": 1, "title": "One", "duration": 100,
         "filename": "CD1/t01.flac", "disc_number": 1},
        {"track_number": 2, "title": "Two", "duration": 100,
         "filename": "CD2/t02.flac", "disc_number": 2},
    ])
    result = _do_confirm(payload, uid)
    assert result["image_errors"] == []
    assert _folder_sha256(src) == before_hash

    rec = db.session.get(Recording, result["recording_id"])
    images = db.session.query(RecordingImage).filter_by(recording_id=rec.id).all()
    assert len(images) == 1
    img = images[0]
    assert img.origin == "folder"
    assert img.source_ref == "CD1/folder.jpg"
    assert img.is_primary is True


# ── (e) no images at all ─────────────────────────────────────────────────────

def test_no_images_at_all(app, db, tmp_path):
    from app.api.ingest import _do_confirm

    lib = tmp_path / "lib_none"
    src = lib / "No Image Act" / "Show1"
    src.mkdir(parents=True)
    _silent_flac(src / "t01.flac")

    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["DATA_DIR"] = str(tmp_path / "data_none")
    uid = db.session.query(User).first().id

    before_hash = _folder_sha256(src)

    payload = _confirm_payload(src, "No Image Act",
                                [{"track_number": 1, "title": "One",
                                  "duration": 100, "filename": "t01.flac"}])
    result = _do_confirm(payload, uid)
    assert result["image_errors"] == []
    assert _folder_sha256(src) == before_hash

    rec = db.session.get(Recording, result["recording_id"])
    images = db.session.query(RecordingImage).filter_by(recording_id=rec.id).all()
    assert images == []

    # No art at all must not create an empty images directory either.
    from app.utils.entity_images import image_dir
    with app.app_context():
        d = image_dir("recordings", str(rec.id))
    assert not d.exists()


# ── discover_folder_images() / extract_embedded_pictures() unit tests ───────

def test_discover_folder_images_orders_front_first(tmp_path):
    from app.utils.recording_images import discover_folder_images

    folder = tmp_path / "show"
    folder.mkdir()
    (folder / "back.png").write_bytes(_PNG_BYTES)
    (folder / "cover.jpg").write_bytes(_JPEG_BYTES)
    (folder / "Artwork.PNG").write_bytes(_PNG_BYTES)

    result = discover_folder_images(str(folder))
    names = [r for r, _is_front in result]
    fronts = [is_front for _r, is_front in result]

    assert names[0] in ("Artwork.PNG", "cover.jpg")
    assert fronts[0] is True and fronts[1] is True
    assert fronts[-1] is False
    assert names[-1] == "back.png"


def test_discover_folder_images_never_descends_into_underscore_or_dot_dirs(tmp_path):
    from app.utils.recording_images import discover_folder_images

    folder = tmp_path / "show2"
    (folder / "_scans").mkdir(parents=True)
    (folder / "_scans" / "cover.jpg").write_bytes(_JPEG_BYTES)
    (folder / ".appledouble").mkdir(parents=True)
    (folder / ".appledouble" / "cover.jpg").write_bytes(_JPEG_BYTES)

    assert discover_folder_images(str(folder)) == []


def test_extract_embedded_pictures_stops_after_first_hit(tmp_path):
    from app.utils.recording_images import extract_embedded_pictures

    f1 = tmp_path / "t01.flac"
    f2 = tmp_path / "t02.flac"
    _silent_flac(f1)  # no picture
    _silent_flac(f2, picture_bytes=_JPEG_BYTES, picture_type=3)

    # Bare paths (no nested-disc case here) -- file_path falls back to the
    # basename, same as every un-nested caller before the nested-path fix.
    result = extract_embedded_pictures([str(f1), str(f2)])
    assert len(result) == 1
    data, ext, is_front, source_ref = result[0]
    assert data == _JPEG_BYTES
    assert ext == "jpg"
    assert is_front is True
    assert source_ref == "embedded:t02.flac"


def test_extract_embedded_pictures_source_ref_uses_nested_file_path(tmp_path):
    """
    Fix from chunk 4 (Studio Records spec v1): source_ref must be
    "embedded:<track file_path>" using the track's own folder-relative
    file_path, which can be nested for a multi-disc show ("CD1/01.flac"),
    NOT the abs path's basename ("01.flac") -- two discs' track 1 share a
    basename but never a file_path, so basename-keyed dedupe would conflate
    them.
    """
    from app.utils.recording_images import extract_embedded_pictures

    cd1 = tmp_path / "CD1" / "01.flac"
    cd2 = tmp_path / "CD2" / "01.flac"
    _silent_flac(cd1)  # no picture -- this disc's track 1 has none
    _silent_flac(cd2, picture_bytes=_JPEG_BYTES, picture_type=3)

    result = extract_embedded_pictures([
        (str(cd1), "CD1/01.flac"),
        (str(cd2), "CD2/01.flac"),
    ])
    assert len(result) == 1
    _data, _ext, is_front, source_ref = result[0]
    assert is_front is True
    assert source_ref == "embedded:CD2/01.flac"


# ── Independent review v1, 2026-09-27: S3 (re-scan primary) / S4 (dot-files
#    and bad-magic bytes) ────────────────────────────────────────────────────

def test_appledouble_and_dotfile_never_discovered_s4(tmp_path):
    """S4: a `._cover.jpg` AppleDouble sidecar (present on every SMB-served
    Mac folder) has a `.jpg` extension and would pass the old extension-only
    filter, but it is not an image -- dot- and underscore-prefixed FILES at
    the folder root must be skipped exactly like the subfolder rule already
    skips dot/underscore-prefixed DIRECTORIES."""
    from app.utils.recording_images import discover_folder_images

    folder = tmp_path / "show3"
    folder.mkdir()
    (folder / "._cover.jpg").write_bytes(_JPEG_BYTES)
    (folder / "_hidden.png").write_bytes(_PNG_BYTES)
    (folder / "cover.jpg").write_bytes(_JPEG_BYTES)

    names = [r for r, _is_front in discover_folder_images(str(folder))]
    assert names == ["cover.jpg"]


def test_bad_magic_bytes_never_stored_s4(app, db, tmp_path):
    """S4: a file with an image extension but non-image bytes (mislabeled,
    corrupt, or a text file saved as '.png') must be skipped, not written to
    disk with whatever extension its filename happened to carry."""
    from app.models.recording import Recording
    from app.utils.recording_images import ingest_recording_images

    lib = tmp_path / "lib"
    src = lib / "Act" / "Show"
    src.mkdir(parents=True)
    _silent_flac(src / "t01.flac")
    (src / "cover.jpg").write_bytes(_JPEG_BYTES)
    (src / "notanimage.png").write_bytes(b"this is text pretending to be a png")
    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["DATA_DIR"] = str(tmp_path / "data")

    uid = _db.session.query(User).first().id
    from app.api.ingest import _do_confirm
    res = _do_confirm(_confirm_payload(src, "Bad Magic Act",
                      [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}]), uid)
    rec = _db.session.get(Recording, res["recording_id"])
    imgs = _db.session.query(RecordingImage).filter_by(recording_id=rec.id).all()
    refs = {i.source_ref for i in imgs}
    assert "cover.jpg" in refs
    assert "notanimage.png" not in refs
    assert len(imgs) == 1


def test_rescan_never_steals_primary_from_upload_s3(app, db, tmp_path):
    """S3: a hand-picked primary (an upload, or an earlier scan's own
    fallback pick) must survive a later re-scan that finds a new image --
    re-ingesting a folder is not the same thing as a person changing their
    mind about the cover."""
    from app.models.recording import Recording
    from app.utils.entity_images import set_primary
    from app.utils.recording_images import ingest_recording_images

    lib = tmp_path / "lib"
    src = lib / "Act" / "Show"
    src.mkdir(parents=True)
    _silent_flac(src / "t01.flac")
    (src / "back.png").write_bytes(_PNG_BYTES)   # only a non-front fallback
    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["DATA_DIR"] = str(tmp_path / "data")

    uid = _db.session.query(User).first().id
    from app.api.ingest import _do_confirm
    res = _do_confirm(_confirm_payload(src, "Rescan Act",
                      [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}]), uid)
    rec = _db.session.get(Recording, res["recording_id"])

    # A person uploads the real cover and makes it primary by hand.
    upload = RecordingImage(recording_id=rec.id, filename="up.jpg", ext=".jpg", origin="upload")
    _db.session.add(upload)
    _db.session.flush()
    set_primary(upload)
    _db.session.commit()

    # The collector later drops another image into the folder; a re-scan
    # must not move primary off the upload.
    (src / "booklet.jpg").write_bytes(_JPEG_BYTES)
    ingest_recording_images(rec, str(lib))
    _db.session.commit()

    imgs = _db.session.query(RecordingImage).filter_by(recording_id=rec.id).all()
    primaries = [i.origin for i in imgs if i.is_primary]
    assert primaries == ["upload"]
