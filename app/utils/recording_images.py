"""
app/utils/recording_images.py — recording-level artwork (Studio Records spec
v1, chunk 4).

Three sources, in priority order (design spec section 3): image files sitting
in the recording folder, pictures embedded in the audio itself (FLAC PICTURE
blocks, ID3 APIC frames), and manual upload (upload reuses the existing
entity_images.py machinery unchanged -- nothing here is needed for it).

Trellis never writes into the collector's folder: every function below only
READS the recording folder. The bytes it finds are copied to
DATA_DIR/images/recordings/<id>/ (via image_dir()), never written back.

Errors are per-image and always swallowed here -- a bad JPEG or an unreadable
FLAC picture block must not fail the ingest it rides in on (design spec
section 3, "Cost at bulk ingest").
"""

import logging
import os
import secrets
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3

from app.extensions import db
from app.utils.entity_images import image_dir, set_primary

logger = logging.getLogger(__name__)

# Extensions discover_folder_images() looks for at the recording folder root.
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# Case-insensitive stems that mark an image as the front cover rather than
# just "some other image in the folder" (back, booklet, flyer, taper art).
_FRONT_STEMS = {"cover", "folder", "front", "album", "artwork"}

# mutagen picture/APIC MIME type -> the extension we store it under. Only the
# four types entity_images.py already knows how to serve are accepted; an
# embedded picture in anything else (e.g. image/bmp) is skipped and logged,
# same as an unsupported upload.
_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/jpg":  "jpg",
    "image/png":  "png",
    "image/webp": "webp",
}

# ID3 PictureType.COVER_FRONT / FLAC Picture.type == 3 -- both mutagen's FLAC
# Picture and its ID3 APIC frame use the same numeric enumeration (the ID3
# APIC picture-type table), so one constant covers both formats.
_FRONT_PICTURE_TYPE = 3


def _is_hidden_or_underscored(name):
    return name.startswith("_") or name.startswith(".")


def _list_images_in(dir_path):
    """Image files (by extension) directly inside dir_path, name-sorted.

    Skips dot- and underscore-prefixed names -- an SMB-served Mac folder
    scatters AppleDouble sidecars (`._cover.jpg`) that pass the extension
    check but are not images at all (S4). The subfolder walk in
    discover_folder_images() already applies this same rule to directory
    names; this applies it to the files themselves.
    """
    try:
        entries = sorted(os.listdir(dir_path))
    except OSError:
        return []
    out = []
    for name in entries:
        if _is_hidden_or_underscored(name):
            continue
        full = os.path.join(dir_path, name)
        if not os.path.isfile(full):
            continue
        if os.path.splitext(name)[1].lower() in _IMG_EXTS:
            out.append(name)
    return out


# Magic bytes for the four formats entity_images.py knows how to serve.
# discover_folder_images() only filters by extension, so a mislabeled or
# corrupt file (e.g. a text file saved as ".png") must still be caught before
# it is written to disk (S4).
def _sniff_ext(data_bytes):
    """The real image format of `data_bytes` by magic, or None if none match."""
    if data_bytes[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data_bytes[:4] == b"RIFF" and data_bytes[8:12] == b"WEBP":
        return "webp"
    return None


def _front_sort_key(name):
    stem = os.path.splitext(name)[0].lower()
    # Front-candidate stems first (alphabetical among themselves), then
    # everything else alphabetical -- matches "ordered front first then by
    # name" (design spec section 3 / build plan chunk 4).
    return (0 if stem in _FRONT_STEMS else 1, name.lower())


def discover_folder_images(folder_abs):
    """
    Image files in a recording folder, per design spec section 3 rule 1.

    Returns a list of (rel_path, is_front) tuples, rel_path relative to
    folder_abs using '/' regardless of platform, front candidates first.

    Looks only at the folder root. Never descends into a '_'- or
    '.'-prefixed subfolder. When the root itself has no images, falls back
    to the first eligible subfolder (name-sorted) that has any -- covers a
    multi-disc folder (CD1/CD2) whose only cover art sits inside disc 1.
    Does not recurse past that one level.
    """
    root_images = _list_images_in(folder_abs)
    rel_prefix = ""

    if not root_images:
        try:
            subdirs = sorted(
                d for d in os.listdir(folder_abs)
                if os.path.isdir(os.path.join(folder_abs, d))
                and not _is_hidden_or_underscored(d)
            )
        except OSError:
            subdirs = []
        for d in subdirs:
            candidate = os.path.join(folder_abs, d)
            imgs = _list_images_in(candidate)
            if imgs:
                root_images = imgs
                rel_prefix = d
                break

    ordered = sorted(root_images, key=_front_sort_key)
    out = []
    for name in ordered:
        stem = os.path.splitext(name)[0].lower()
        is_front = stem in _FRONT_STEMS
        rel = f"{rel_prefix}/{name}" if rel_prefix else name
        out.append((rel, is_front))
    return out


def extract_embedded_pictures(track_pairs):
    """
    Embedded cover art from a recording's audio files, per design spec
    section 3 rule 2.

    `track_pairs` is a list of (abs_path, file_path) tuples, tracks in
    track_number order -- file_path is the track's own folder-relative path
    (Track.file_path), which for a multi-disc show can be nested, e.g.
    "CD1/01.flac". A bare list of absolute paths is accepted too, for
    call sites (and the unit tests below) that have no nested-disc case to
    worry about: file_path then falls back to that path's own basename.

    Returns a list of (bytes, ext, is_front, source_ref) tuples. Only the
    FIRST file (in the given order) that yields any picture is used; a file
    with none is skipped in favor of the next one. Within that one file,
    FLAC PICTURE blocks and ID3 APIC frames of type 3 (front cover) sort
    first.

    source_ref is "embedded:<file_path>" -- NOT the basename. A nested
    multi-disc track's file_path (e.g. "CD1/01.flac") is what dedupe
    (RecordingImage.source_ref) and re-ingest actually key on, and two
    different discs' track 1 share a basename ("01.flac") but never a
    file_path.
    """
    for pair in track_pairs:
        if isinstance(pair, (tuple, list)):
            path, file_path = pair
        else:
            path, file_path = pair, os.path.basename(pair)
        ext = os.path.splitext(path)[1].lower()
        raw = []  # (bytes, mime, picture_type)
        try:
            if ext == ".flac":
                audio = FLAC(path)
                for pic in audio.pictures:
                    raw.append((pic.data, pic.mime, pic.type))
            elif ext == ".mp3":
                tags = ID3(path)
                for frame in tags.getall("APIC"):
                    raw.append((frame.data, frame.mime, frame.type))
        except Exception:  # noqa: BLE001
            logger.exception("recording artwork: could not read embedded pictures from %s", path)
            raw = []

        if not raw:
            continue

        pics = []
        for data, mime, ptype in raw:
            out_ext = _MIME_TO_EXT.get((mime or "").lower())
            if out_ext is None:
                logger.warning("recording artwork: skipping embedded picture with "
                                "unsupported mime %r in %s", mime, path)
                continue
            pics.append((data, out_ext, ptype == _FRONT_PICTURE_TYPE))

        if not pics:
            continue

        pics.sort(key=lambda p: 0 if p[2] else 1)
        return [(data, out_ext, is_front, f"embedded:{file_path}")
                for data, out_ext, is_front in pics]

    return []


def ingest_recording_images(rec, library_root):
    """
    Populate rec.images from its folder and its tracks' embedded pictures.

    Adds RecordingImage rows and flushes (does not commit -- the caller owns
    the transaction, matching every other mutation helper in this codebase).
    Never raises: every per-image failure is caught, logged, and skipped, and
    an overall failure to even read the folder is caught the same way, so an
    artwork problem never blocks the ingest it rides in on.

    Returns the number of RecordingImage rows created (0 if none, including
    on any error).
    """
    from app.models.recording_image import RecordingImage

    folder_abs = os.path.join(str(library_root), rec.folder_path)

    existing_refs = {
        img.source_ref for img in
        db.session.query(RecordingImage.source_ref)
        .filter_by(recording_id=rec.id).all()
    }
    # Captured before this scan adds anything -- a re-scan must not steal
    # primary from a row that was already primary (S3), and checking
    # rec.images later would also see this call's own new rows.
    already_has_primary = (
        db.session.query(RecordingImage.id)
        .filter_by(recording_id=rec.id, is_primary=True)
        .first() is not None
    )
    next_order = 0
    created = []
    front_img = None
    images_dir = None  # lazily resolved -- a recording with no art at all
                        # should never create an empty directory.

    def _store(data_bytes, ext, origin, source_ref):
        nonlocal images_dir, next_order
        if source_ref in existing_refs:
            return None
        real_ext = _sniff_ext(data_bytes)
        if real_ext is None:
            logger.warning("recording artwork: skipping non-image bytes "
                            "(recording=%s, source_ref=%s)", rec.id, source_ref)
            return None
        ext = real_ext
        try:
            if images_dir is None:
                images_dir = image_dir("recordings", str(rec.id))
                images_dir.mkdir(parents=True, exist_ok=True)
            fname = f"img_{secrets.token_hex(6)}.{ext}"
            (images_dir / fname).write_bytes(data_bytes)
            img = RecordingImage(
                recording_id=rec.id,
                filename=fname,
                ext=f".{ext}",
                origin=origin,
                source_ref=source_ref,
                sort_order=next_order,
            )
            next_order += 1
            db.session.add(img)
            existing_refs.add(source_ref)
            return img
        except Exception:  # noqa: BLE001
            logger.exception("recording artwork: could not store image "
                              "(recording=%s, source_ref=%s)", rec.id, source_ref)
            return None

    # ── 1. Folder images ─────────────────────────────────────────────────
    try:
        for rel_path, is_front in discover_folder_images(folder_abs):
            try:
                abs_path = os.path.join(folder_abs, rel_path)
                ext = os.path.splitext(rel_path)[1].lstrip(".").lower()
                if ext == "jpeg":
                    ext = "jpg"
                data = Path(abs_path).read_bytes()
            except Exception:  # noqa: BLE001
                logger.exception("recording artwork: could not read folder "
                                  "image %s for recording %s", rel_path, rec.id)
                continue
            img = _store(data, ext, "folder", rel_path)
            if img:
                created.append(img)
                if is_front and front_img is None:
                    front_img = img
    except Exception:  # noqa: BLE001
        logger.exception("recording artwork: folder discovery failed for recording %s", rec.id)

    # ── 2. Embedded pictures ─────────────────────────────────────────────
    try:
        tracks = sorted(rec.tracks, key=lambda t: (t.track_number is None, t.track_number or 0))
        track_pairs = [(os.path.join(folder_abs, t.file_path), t.file_path)
                       for t in tracks if t.file_path]
        for data, ext, is_front, source_ref in extract_embedded_pictures(track_pairs):
            img = _store(data, ext, "embedded", source_ref)
            if img:
                created.append(img)
                if is_front and front_img is None:
                    front_img = img
    except Exception:  # noqa: BLE001
        logger.exception("recording artwork: embedded extraction failed for recording %s", rec.id)

    # ── 3. Primary ────────────────────────────────────────────────────────
    # First front candidate wins (folder before embedded, matching the loop
    # order above); if nothing was flagged front, the first image found at
    # all becomes primary, same fallback primary_for() uses.
    #
    # But only when the recording has no primary yet. A re-scan that finds a
    # new image must never steal primary from one a person already chose
    # (upload, or an earlier scan) -- that is what "primary" means (S3).
    if not already_has_primary:
        if front_img is None and created:
            front_img = created[0]
        if front_img is not None:
            set_primary(front_img)

    db.session.flush()
    return len(created)
