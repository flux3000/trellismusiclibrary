"""
tests/test_ingest_rescan_and_genre.py — Add Recording's two new server-side
behaviours (2026-09-01).

1. RESCAN. `parse_info_file(text=...)` and `build_scan_payload(info_override=)`
   let the Rescan button re-run the inference over an info file the reviewer
   has EDITED but not saved. The point of the override is that nothing is
   written to the collector's disk — "try again" must not silently modify a
   source folder — so the tests below check the disk is untouched as much as
   they check the parse.

2. GENRE ON THE PERFORMER. The ingest form can now set an act's genre. Genre
   lives on Performer, not Recording, and this is the only place outside
   api/genres.py that can create one, so the guards matter more than the happy
   path: an id beats a name, a name matches case-insensitively before creating,
   and a payload that says NOTHING about genre must not clear an existing one.
   That last case is the one that would silently strip the genre off every act
   Batch Import and Auto-Ingest touch, neither of which visits this form.
"""

import os

import pytest

from app.models.genre import Genre
from app.models.performer import Performer
from app.utils.ingest import parse_info_file, build_scan_payload


INFO_TEXT = """Grateful Dead
May 8, 1977
Barton Hall, Cornell University
Ithaca, NY

01. Minglewood Blues
02. Loser
03. El Paso
"""

EDITED_TEXT = """Grateful Dead
May 9, 1977
Buffalo Memorial Auditorium
Buffalo, NY

01. Help On The Way
02. Slipknot
"""


# ── parse_info_file(text=...) ────────────────────────────────────────────────

def test_parse_from_text_matches_parse_from_disk(tmp_path):
    """The override is a different SOURCE for the same bytes, not a different
    parser. If these two ever disagree, the rescan path has quietly forked."""
    f = tmp_path / "info.txt"
    f.write_text(INFO_TEXT, encoding="utf-8")

    from_disk = parse_info_file(str(f))
    from_text = parse_info_file(None, text=INFO_TEXT)

    for key in ("artist", "year", "month", "day", "venue", "city", "state"):
        assert from_disk.get(key) == from_text.get(key), key
    assert [t["title"] for t in from_disk["tracks"]] == \
           [t["title"] for t in from_text["tracks"]]


def test_text_wins_over_the_file_and_the_file_is_not_read(tmp_path):
    f = tmp_path / "info.txt"
    f.write_text(INFO_TEXT, encoding="utf-8")

    parsed = parse_info_file(str(f), text=EDITED_TEXT)
    assert parsed["day"] == 9
    assert "Buffalo" in (parsed.get("venue") or parsed.get("city") or "")
    assert [t["title"] for t in parsed["tracks"]] == ["Help on the Way", "Slipknot"]


def test_empty_text_is_a_value_not_a_fallback(tmp_path):
    """
    A reviewer who cleared the box means the file is empty. Falling back to the
    file on a falsy check would silently reinstate text they deleted — hence
    `is not None` rather than truthiness in parse_info_file.
    """
    f = tmp_path / "info.txt"
    f.write_text(INFO_TEXT, encoding="utf-8")

    parsed = parse_info_file(str(f), text="")
    assert parsed["raw_content"] == ""
    assert parsed["tracks"] == []
    assert parsed.get("artist") in (None, "")


def test_missing_file_still_returns_a_shape(tmp_path):
    """Pre-existing behaviour, pinned because the new branch sits beside it."""
    parsed = parse_info_file(str(tmp_path / "nope.txt"))
    assert parsed == {"raw_content": "", "tracks": []}


# ── build_scan_payload(info_override=...) ────────────────────────────────────

def _folder_with_audio(tmp_path, info=INFO_TEXT, info_name="info.txt"):
    d = tmp_path / "gd1977-05-08"
    d.mkdir()
    # scan_folder classifies by extension; the bytes never have to be real FLAC
    # for the text-file half of the payload, which is all these tests read.
    (d / "01.flac").write_bytes(b"fLaC")
    (d / "02.flac").write_bytes(b"fLaC")
    if info is not None:
        (d / info_name).write_text(info, encoding="utf-8")
    return d


def test_override_changes_the_suggestions_without_touching_disk(app, tmp_path):
    d = _folder_with_audio(tmp_path)
    before = (d / "info.txt").read_text(encoding="utf-8")

    plain = build_scan_payload(str(d))
    assert plain["suggestions"]["from_info_file"]["day"] == 8

    edited = build_scan_payload(str(d), info_override={
        "filename": "info.txt", "content": EDITED_TEXT})
    assert edited["suggestions"]["from_info_file"]["day"] == 9
    assert [t["title"] for t in edited["suggestions"]["from_info_file"]["tracks"]] \
           == ["Help on the Way", "Slipknot"]

    # The whole reason the override exists: the collector's file is the taper's
    # own words and a rescan is a read.
    assert (d / "info.txt").read_text(encoding="utf-8") == before


def test_default_is_byte_identical_to_no_override(app, tmp_path):
    """Batch import passes nothing and must be completely unaffected."""
    d = _folder_with_audio(tmp_path)
    assert build_scan_payload(str(d))["suggestions"] == \
           build_scan_payload(str(d), info_override=None)["suggestions"]


def test_override_only_touches_the_named_candidate(app, tmp_path):
    """
    Applying the text to every candidate would make the Details panel's file
    switcher show one file's contents under every filename.
    """
    d = _folder_with_audio(tmp_path)
    (d / "notes.txt").write_text("Some other file entirely.\n", encoding="utf-8")

    scan = build_scan_payload(str(d), info_override={
        "filename": "info.txt", "content": EDITED_TEXT})
    by_name = {c["filename"]: c for c in scan["text_file_candidates"]}
    assert "Help On The Way" in by_name["info.txt"]["content"]   # raw text, not a parsed title
    assert "Some other file entirely" in by_name["notes.txt"]["content"]


def test_typed_from_scratch_when_the_folder_has_no_text_file(app, tmp_path):
    """
    A folder with no info file at all, where the reviewer typed one. There is
    no candidate to override, so the text becomes one — otherwise Rescan
    silently ignores everything they wrote, and "the parser found nothing" is a
    different and far more misleading answer than the truth.
    """
    d = _folder_with_audio(tmp_path, info=None)
    scan = build_scan_payload(str(d), info_override={
        "filename": None, "content": EDITED_TEXT})

    assert scan["suggestions"]["from_info_file"]["day"] == 9
    assert scan["text_file_candidates"][0]["filename"] == "info.txt"
    # Still nothing written — a rescan never creates a file either.
    assert not os.path.exists(str(d / "info.txt"))


def test_blank_override_on_an_empty_folder_adds_no_candidate(app, tmp_path):
    d = _folder_with_audio(tmp_path, info=None)
    scan = build_scan_payload(str(d), info_override={"filename": None, "content": "   "})
    assert scan["text_file_candidates"] == []


# ── Genre on the Performer, through /api/ingest/confirm ─────────────────────

@pytest.fixture()
def api(app):
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


# The confirm endpoint runs a background job that copies real files, so these
# exercise `_apply_performer_genre` directly. That is the honest boundary: the
# function is the whole of the genre decision, and driving it through a
# file-copying job would test the copier, not the rule.


def test_genre_id_links_an_existing_genre(app):
    from app.extensions import db as _db
    from app.api.ingest import _apply_performer_genre

    g = Genre(name="Bluegrass", color="#7a8b99")
    p = Performer(name="Hot Rize")
    _db.session.add_all([g, p])
    _db.session.commit()

    _apply_performer_genre(p, {"genre_id": g.id})
    _db.session.commit()
    assert p.genre_id == g.id


def test_genre_name_matches_case_insensitively_before_creating(app):
    from app.extensions import db as _db
    from app.api.ingest import _apply_performer_genre

    g = Genre(name="Bluegrass")
    p = Performer(name="Hot Rize")
    _db.session.add_all([g, p])
    _db.session.commit()
    before = _db.session.query(Genre).count()

    _apply_performer_genre(p, {"genre_name": "bluegrass"})
    _db.session.commit()

    assert p.genre_id == g.id
    assert _db.session.query(Genre).count() == before, "created a twin"


def test_genre_name_creates_when_genuinely_new(app):
    from app.extensions import db as _db
    from app.api.ingest import _apply_performer_genre

    p = Performer(name="The Meters")
    _db.session.add(p)
    _db.session.commit()

    _apply_performer_genre(p, {"genre_name": "New Orleans Funk"})
    _db.session.commit()

    assert p.genre is not None
    assert p.genre.name == "New Orleans Funk"


def test_genre_id_wins_over_genre_name(app):
    from app.extensions import db as _db
    from app.api.ingest import _apply_performer_genre

    g = Genre(name="Bluegrass")
    p = Performer(name="Hot Rize")
    _db.session.add_all([g, p])
    _db.session.commit()

    _apply_performer_genre(p, {"genre_id": g.id, "genre_name": "Something Else"})
    _db.session.commit()

    assert p.genre_id == g.id
    assert _db.session.query(Genre).filter_by(name="Something Else").first() is None


def test_silence_never_clears_an_existing_genre(app):
    """
    ⚠ The one that matters. Batch Import and Auto-Ingest never visit the review
    form and send neither key. Treating that omission as "clear it" would strip
    the genre off every act they touch — the same None-vs-[] trap the
    members/guests payload documents.
    """
    from app.extensions import db as _db
    from app.api.ingest import _apply_performer_genre

    g = Genre(name="Bluegrass")
    p = Performer(name="Hot Rize")
    _db.session.add_all([g, p])
    _db.session.commit()
    p.genre_id = g.id
    _db.session.commit()

    _apply_performer_genre(p, {})                       # no keys at all
    _apply_performer_genre(p, {"genre_id": None, "genre_name": ""})
    _db.session.commit()

    assert p.genre_id == g.id


def test_unknown_genre_id_is_ignored_rather_than_fatal(app):
    """A stale id must not 500 an ingest that is otherwise fine."""
    from app.extensions import db as _db
    from app.api.ingest import _apply_performer_genre

    p = Performer(name="The Meters")
    _db.session.add(p)
    _db.session.commit()

    _apply_performer_genre(p, {"genre_id": 999999})
    _db.session.commit()
    assert p.genre_id is None
