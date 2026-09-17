"""
tests/test_db_logic.py — serializer, tag builder, and cascade-prune behavior
against the seeded temp DB.

(2026-07-11 remodel: Artist = act; Musician = person; Membership = M2M.)
"""

import json as _json
import pytest

from app.extensions import db as _db
from app.models.recording import Recording
from app.models.performance import Performance
from app.models.artist import Artist
from app.models.musician import Musician, Membership
from app.models.venue import Venue
from app.models.track import Track
from app.models.track_analysis import TrackAnalysis
from app.models.play_log import PlayLog
from app.utils.serialize import recording_summary, recording_row
from app.utils.ingest import build_recording_tags, scan_folder, compute_audio_rename_map
from app.utils.pruning import prune_after_recording_delete, prune_artist_if_orphaned


@pytest.fixture()
def api(app):
    """A test client with auth disabled — exercises the JSON CRUD endpoints."""
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


def test_recording_summary_shape(app, seeded_ids):
    rec = _db.session.get(Recording, seeded_ids["recording_id"])
    s = recording_summary(rec)
    assert s["source"] == "AUD"
    assert s["quality"] == "B+"
    assert s["track_count"] == 2
    assert s["duration_sec"] == 360        # 300 + 60
    # `listening_quality` (automated, 0–100, audio only) sits alongside
    # `quality` (the manual letter grade covering performance too). Two fields
    # on purpose — neither replaces the other. None until analysed.
    assert s["listening_quality"] is None
    # `is_favorite` is the third independent signal (2026-07-31): a one-click
    # human highlight, owing nothing to either the letter grade or the automated
    # score. Defaults off and analysis never sets it.
    assert s["is_favorite"] is False
    # "rating" left this set 2026-08-18 — the 0-100 manual score is retired from
    # every payload (the DB column survives unread). Three quality signals is the
    # design: the letter grade, the automated score, and the favourite flag.
    assert set(s.keys()) == {"id", "source", "quality", "listening_quality",
                             "is_favorite", "is_complete",
                             "is_official", "track_count", "duration_sec",
                             "created_at"}


def test_build_recording_tags(app, seeded_ids):
    rec = _db.session.get(Recording, seeded_ids["recording_id"])
    tags, total = build_recording_tags(rec)
    assert tags["ARTIST"] == "Bill Evans"
    assert tags["DATE"] == "1980-02-22"
    assert tags["VENUE"] == "Sprague Memorial Hall"
    assert tags["LOCATION"] == "New Haven, CT, US"
    assert total == "2"


def test_performance_event_association(api, seeded_ids):
    """GET /api/performances/<id> surfaces event_id/event_name, and PUT accepts
    event_id (2026-07-13 addition — needed so the recording page can link a
    show to a Festival/Event the same way Add Recording already does)."""
    perf_id = seeded_ids["performance_id"]

    resp = api.get(f"/api/performances/{perf_id}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["event_id"] is None
    assert body["event_name"] is None

    created = api.post("/api/events/", json={"name": "Bonnaroo 2009"})
    assert created.status_code == 201
    event_id = created.get_json()["id"]

    updated = api.put(f"/api/performances/{perf_id}", json={"event_id": event_id})
    assert updated.status_code == 200

    resp = api.get(f"/api/performances/{perf_id}")
    body = resp.get_json()
    assert body["event_id"] == event_id
    assert body["event_name"] == "Bonnaroo 2009"


def test_recording_ai_research_serialization(api, seeded_ids):
    """GET /api/recordings/<id> surfaces the persisted AI Assist blob (2026-07-13
    revival of ai_research_json — null until a research pass has run, then the
    parsed JSON of the latest run)."""
    rec_id = seeded_ids["recording_id"]

    resp = api.get(f"/api/recordings/{rec_id}")
    assert resp.status_code == 200
    assert resp.get_json()["ai_research"] is None

    rec = _db.session.get(Recording, rec_id)
    rec.ai_research_json = '{"thinking": "test", "proposals": []}'
    _db.session.commit()

    resp = api.get(f"/api/recordings/{rec_id}")
    assert resp.get_json()["ai_research"] == {"thinking": "test", "proposals": []}


def test_prune_after_delete_removes_full_chain(app, db, seeded_ids):
    rec_id       = seeded_ids["recording_id"]
    perf_id      = seeded_ids["performance_id"]
    artist_id = seeded_ids["artist_id"]
    musician_id    = seeded_ids["musician_id"]      # the sole member (person)
    track_ids = [t.id for t in Track.query.filter_by(recording_id=rec_id).all()]

    # Simulate delete_recording's child cleanup, then prune.
    db.session.query(TrackAnalysis).filter(TrackAnalysis.track_id.in_(track_ids)).delete(
        synchronize_session=False)
    db.session.query(PlayLog).filter(PlayLog.track_id.in_(track_ids)).delete(
        synchronize_session=False)
    db.session.query(Track).filter(Track.id.in_(track_ids)).delete(synchronize_session=False)
    db.session.query(Recording).filter_by(id=rec_id).delete(synchronize_session=False)
    db.session.flush()

    pruned = prune_after_recording_delete(perf_id)
    db.session.commit()

    assert pruned == {"performances": [perf_id], "artists": [artist_id],
                      "musicians": [musician_id]}
    assert _db.session.get(Performance, perf_id) is None
    assert _db.session.get(Artist, artist_id) is None
    assert _db.session.get(Musician, musician_id) is None      # orphaned person removed
    assert TrackAnalysis.query.count() == 0
    assert PlayLog.query.count() == 0


def test_prune_keeps_person_who_is_in_another_act(app, db, seeded_ids):
    artist_id = seeded_ids["artist_id"]
    musician_id    = seeded_ids["musician_id"]
    rec_id       = seeded_ids["recording_id"]

    # Put the same person in a second artist, so they survive the prune.
    other = Artist(name="Bill Evans Trio")
    db.session.add(other); db.session.flush()
    db.session.add(Membership(artist_id=other.id, musician_id=musician_id, order=0))
    db.session.flush()

    # Orphan the original artist (remove its performance) and prune it.
    # Must clear the seeded Recording/Track chain first (as delete_recording's
    # real cleanup does) — with FK enforcement on, bulk-deleting Performance
    # while a Recording still references it violates the FK.
    track_ids = [t.id for t in Track.query.filter_by(recording_id=rec_id).all()]
    db.session.query(TrackAnalysis).filter(TrackAnalysis.track_id.in_(track_ids)).delete(
        synchronize_session=False)
    db.session.query(PlayLog).filter(PlayLog.track_id.in_(track_ids)).delete(
        synchronize_session=False)
    db.session.query(Track).filter(Track.id.in_(track_ids)).delete(synchronize_session=False)
    db.session.query(Recording).filter_by(id=rec_id).delete(synchronize_session=False)
    db.session.query(Performance).filter_by(artist_id=artist_id).delete(
        synchronize_session=False)
    db.session.flush()
    result = prune_artist_if_orphaned(artist_id)
    db.session.commit()

    assert result["artists"] == [artist_id]
    assert result["musicians"] == []                          # person kept
    assert _db.session.get(Musician, musician_id) is not None
    assert _db.session.get(Artist, other.id) is not None


# ── CRUD endpoint guards (delete refuses while referenced) ─────────────────────

def test_delete_artist_refuses_with_recordings(api, seeded_ids):
    r = api.delete(f"/api/artists/{seeded_ids['artist_id']}")
    assert r.status_code == 409
    assert "performance" in r.get_json()["error"]
    assert _db.session.get(Artist, seeded_ids["artist_id"]) is not None


def test_delete_venue_refuses_with_performances(api, seeded_ids):
    perf = _db.session.get(Performance, seeded_ids["performance_id"])
    r = api.delete(f"/api/venues/{perf.venue_id}")
    assert r.status_code == 409
    assert _db.session.get(Venue, perf.venue_id) is not None


def test_delete_musician_refuses_while_member(api, seeded_ids):
    r = api.delete(f"/api/musicians/{seeded_ids['musician_id']}")
    assert r.status_code == 409
    assert "member" in r.get_json()["error"]


def test_musician_edit_and_delete_when_orphan(api):
    created = api.post("/api/musicians/", json={"name": "Sandip Burman"}).get_json()
    aid = created["id"]
    # Edit
    assert api.put(f"/api/musicians/{aid}", json={"sort_name": "Burman, Sandip"}).status_code == 200
    assert _db.session.get(Musician, aid).sort_name == "Burman, Sandip"
    # Delete (no memberships) succeeds
    assert api.delete(f"/api/musicians/{aid}").status_code == 200
    assert _db.session.get(Musician, aid) is None


def test_do_confirm_copies_files_and_reports_progress(app, db, tmp_path):
    """The background ingest worker copies the folder, creates the chain, and
    reports copy progress ending at 100%. Audio lands flattened + renamed
    ("NN - Title.ext") per the 2026-07-14 always-flatten-and-rename policy;
    non-audio extras (cover.jpg) keep their original name."""
    from app.api.ingest import _do_confirm
    from app.models.user import User

    src = tmp_path / "src_show"; src.mkdir()
    (src / "t01.flac").write_bytes(b"x" * 2000)
    (src / "cover.jpg").write_bytes(b"y" * 1000)   # non-audio extra, copied too
    lib = tmp_path / "lib"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    progress = []
    data = {
        "source_folder_path": str(src),
        "artist_name": "Progress Test Act",
        "start_year": 2020, "start_month": 1, "start_day": 2,
        "source": "AUD",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}],
    }
    result = _do_confirm(data, uid, lambda c, t: progress.append((c, t)))

    assert result["recording_id"]
    rec = _db.session.get(Recording, result["recording_id"])
    track = rec.tracks[0]
    assert track.file_path == "01 - One.flac"
    assert (lib / rec.folder_path / "01 - One.flac").exists()
    assert (lib / rec.folder_path / "cover.jpg").exists()
    assert progress and progress[-1][0] == progress[-1][1]   # finished at 100%


def test_do_confirm_does_not_corrupt_existing_roster(app, db, tmp_path):
    """Regression test for the ingest-time act-roster-corruption bug
    (2026-07-22 fix): confirming a recording for an EXISTING Artist with
    'members' set to (a subset of) its already-resolved lineup must NOT call
    set_artist_members and rewrite the act roster — that was the same
    Phase-1 bug, just never ported to the ingest confirm path. Membership
    stint history must survive untouched, and a 'guests' name must attach as
    performance-level personnel only, never touching the roster."""
    from app.api.ingest import _do_confirm
    from app.models.user import User
    from app.utils.artists import resolve_or_create_artist, add_membership_stint
    from app.utils.personnel import resolve_performance_personnel

    uid = db.session.query(User).first().id
    lib = tmp_path / "lib"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)

    # An existing act with a DATED stint — exactly what set_artist_members'
    # wholesale-rewrite behavior used to be able to destroy.
    artist = resolve_or_create_artist("Existing Act With History")
    stint = add_membership_stint(artist, "Roster Person",
                                  start_year=1970, end_year=1975)
    _db.session.commit()

    src = tmp_path / "src_show"; src.mkdir()
    (src / "t01.flac").write_bytes(b"x" * 500)
    data = {
        "source_folder_path": str(src),
        "artist_name": "Existing Act With History",
        "start_year": 1972, "start_month": 6, "start_day": 1,
        "source": "AUD",
        "members": ["Roster Person"],   # the Add Recording form's pre-populated Members row
        "guests":  ["Sit-In Player"],
        "tracks": [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}],
    }
    result = _do_confirm(data, uid, None)
    assert result["recording_id"]

    # Roster untouched: still exactly one stint, same dates, not duplicated.
    roster = _db.session.query(Membership).filter_by(artist_id=artist.id).all()
    assert len(roster) == 1
    assert roster[0].id == stint.id
    assert (roster[0].start_year, roster[0].end_year) == (1970, 1975)

    rec = _db.session.get(Recording, result["recording_id"])
    resolved = resolve_performance_personnel(rec.performance)
    by_name = {r["name"]: (r["source"], r["is_guest"]) for r in resolved}
    assert by_name == {
        "Roster Person": ("inherited", False),
        "Sit-In Player": ("guest", True),
    }


def test_do_confirm_seeds_roster_for_brand_new_artist(app, db, tmp_path):
    """The other half of the same fix: a BRAND NEW Artist's first-show
    'members' still seeds its initial roster (unchanged behavior) — only
    an EXISTING Artist's roster is protected from being overwritten."""
    from app.api.ingest import _do_confirm
    from app.models.user import User

    uid = db.session.query(User).first().id
    lib = tmp_path / "lib"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)

    src = tmp_path / "src_show"; src.mkdir()
    (src / "t01.flac").write_bytes(b"x" * 500)
    data = {
        "source_folder_path": str(src),
        "artist_name": "Brand New Duo",
        "start_year": 2002, "start_month": 6, "start_day": 23,
        "source": "AUD",
        "members": ["Person A", "Person B"],
        "tracks": [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}],
    }
    result = _do_confirm(data, uid, None)
    assert result["recording_id"]

    artist = _db.session.query(Artist).filter_by(name="Brand New Duo").first()
    roster_names = {m.musician.name for m in
                    _db.session.query(Membership).filter_by(artist_id=artist.id).all()}
    assert roster_names == {"Person A", "Person B"}


def test_do_confirm_omitted_members_key_does_not_wipe_inherited_roster(app, db, tmp_path):
    """Regression test for the Batch Import Auto-Ingest bug (2026-07-23 fix):
    that path never sends "members"/"guests" in its confirm payload at all
    (no review wizard, no pre-fill) — data.get("members") is None, not [].
    _do_confirm used to collapse that None to [] before ever reaching
    sync_performance_personnel(), which reads [] as "the user just cleared
    every member," tripping the case-5 safeguard: flip to personnel_mode=
    'explicit' and snapshot the (empty) surviving lineup. Net effect: an
    existing act's Members row on View Recording came out blank even though
    the artist's own roster was fully intact (Ryan's report — "Bela Fleck
    & Tony Trischka" ingested via Bulk Import's Auto-Ingest button).
    The fix preserves the None/[] distinction through to
    sync_performance_personnel, so an omitted key means "leave the resolved
    lineup exactly as it is" — a true no-op for a brand-new inherit-mode
    performance, since the resolved lineup IS the roster already."""
    from app.api.ingest import _do_confirm
    from app.models.user import User
    from app.utils.artists import resolve_or_create_artist, add_membership_stint
    from app.utils.personnel import resolve_performance_personnel

    uid = db.session.query(User).first().id
    lib = tmp_path / "lib"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)

    artist = resolve_or_create_artist("Auto Ingest Duo")
    add_membership_stint(artist, "Fleck-Like Person")
    add_membership_stint(artist, "Trischka-Like Person")
    _db.session.commit()

    src = tmp_path / "src_show_auto"; src.mkdir()
    (src / "t01.flac").write_bytes(b"x" * 500)
    data = {
        # No "members" / "guests" keys at all — matches _batchIngestOne's
        # payload in app.js, which never visits the review wizard.
        "source_folder_path": str(src),
        "artist_name": "Auto Ingest Duo",
        "start_year": 2002, "start_month": 7, "start_day": 28,
        "source": "AUD",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}],
    }
    result = _do_confirm(data, uid, None)
    rec = _db.session.get(Recording, result["recording_id"])

    assert rec.performance.personnel_mode == "inherit"   # never flipped
    resolved = resolve_performance_personnel(rec.performance)
    names = {r["name"] for r in resolved}
    assert names == {"Fleck-Like Person", "Trischka-Like Person"}


def test_do_confirm_persists_pre_save_ai_result(app, db, tmp_path):
    """A recording ingested after running AI Assist pre-confirm (Add
    Recording's own button, before the row exists) should land with
    ai_research_json already populated — it was previously dropped on the
    floor because the confirm payload never carried it (Ryan, 2026-07-14:
    'the result was not saved with the database submission')."""
    from app.api.ingest import _do_confirm
    from app.models.user import User

    src = tmp_path / "src_show_ai"; src.mkdir()
    (src / "t01.flac").write_bytes(b"x" * 2000)
    lib = tmp_path / "lib_ai"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    ai_result = {
        "thinking": "Date correction is high-confidence...",
        "proposals": [{"field": "date", "proposed": "1974-07-03",
                        "confidence": "high", "source": "web"}],
        "track_titles": [], "verify_items": [], "provenance_notes": [], "sources": [],
    }
    data = {
        "source_folder_path": str(src),
        "artist_name": "Pre-Save AI Act",
        "start_year": 1974, "start_month": 8, "start_day": 13,
        "source": "FM",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}],
        "ai_result": ai_result,
    }
    result = _do_confirm(data, uid, None)
    rec = _db.session.get(Recording, result["recording_id"])
    assert rec.ai_research_json is not None
    saved = _json.loads(rec.ai_research_json)
    assert saved["proposals"][0]["proposed"] == "1974-07-03"


def test_do_confirm_without_ai_result_leaves_ai_research_json_null(app, db, tmp_path):
    """No AI Assist run pre-confirm — should stay null, not error or default
    to some empty-but-truthy blob."""
    from app.api.ingest import _do_confirm
    from app.models.user import User

    src = tmp_path / "src_show_no_ai"; src.mkdir()
    (src / "t01.flac").write_bytes(b"x" * 2000)
    lib = tmp_path / "lib_no_ai"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    data = {
        "source_folder_path": str(src),
        "artist_name": "No AI Act",
        "start_year": 1980, "start_month": 1, "start_day": 1,
        "source": "AUD",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}],
    }
    result = _do_confirm(data, uid, None)
    rec = _db.session.get(Recording, result["recording_id"])
    assert rec.ai_research_json is None


def test_check_existing_no_artist_match(api):
    """Artist name that doesn't exist yet — nothing to warn about."""
    resp = api.get("/api/ingest/check-existing?artist_name=Nobody+At+All&year=1999")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["artist_found"] is False
    assert body["performances"] == []


def test_check_existing_finds_recording_for_artist_and_date(app, db, api, tmp_path):
    """The core case: a recording already exists for this artist+date —
    Add Recording's duplicate-warning check should surface it (non-blocking,
    per Ryan's 2026-07-14 call — this only warns, never blocks Confirm)."""
    from app.api.ingest import _do_confirm
    from app.models.user import User

    src = tmp_path / "src_dup1"; src.mkdir()
    (src / "t01.flac").write_bytes(b"x" * 2000)
    lib = tmp_path / "lib_dup1"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    data = {
        "source_folder_path": str(src),
        "artist_name": "Duplicate Check Trio",
        "start_year": 1974, "start_month": 7, "start_day": 3,
        "source": "FM", "quality": "A",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}],
    }
    _do_confirm(data, uid, None)

    resp = api.get(
        "/api/ingest/check-existing"
        "?artist_name=Duplicate+Check+Trio&year=1974&month=7&day=3"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["artist_found"] is True
    assert len(body["performances"]) == 1
    recs = body["performances"][0]["recordings"]
    assert len(recs) == 1
    assert recs[0]["source"] == "FM"
    assert recs[0]["quality"] == "A"
    assert recs[0]["track_count"] == 1

    # A different day for the same artist/year should NOT match — the
    # whole point is to narrow to the same show, not just the same act/year.
    resp2 = api.get(
        "/api/ingest/check-existing"
        "?artist_name=Duplicate+Check+Trio&year=1974&month=7&day=4"
    )
    assert resp2.get_json()["performances"] == []


def test_check_existing_excludes_performance_with_no_recordings(db, api):
    """A bare Performance row with zero Recordings isn't a duplicate risk —
    nothing to accidentally re-ingest yet."""
    from app.utils.artists import resolve_or_create_artist
    from app.models.performance import Performance

    artist = resolve_or_create_artist("Empty Performance Act")
    perf = Performance(artist_id=artist.id, start_year=2001, start_month=5, start_day=5)
    _db.session.add(perf)
    _db.session.commit()

    resp = api.get("/api/ingest/check-existing?artist_name=Empty+Performance+Act&year=2001")
    body = resp.get_json()
    assert body["artist_found"] is True
    assert body["performances"] == []


def test_do_confirm_verifies_checksums_end_to_end(app, db, tmp_path):
    """A .md5 fingerprint file sitting alongside the audio gets archived,
    parsed, matched to the track by filename, and auto-verified as part of
    confirm — 2026-07-13 checksum feature."""
    import hashlib
    from app.api.ingest import _do_confirm
    from app.models.user import User
    from app.models.track import Track

    src = tmp_path / "src_show2"; src.mkdir()
    audio_bytes = b"fake flac bytes for whole-file md5 test" * 10
    (src / "t01.flac").write_bytes(audio_bytes)
    real_md5 = hashlib.md5(audio_bytes).hexdigest()
    (src / "checksum.md5").write_text(f"{real_md5} *t01.flac\n")
    lib = tmp_path / "lib2"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    data = {
        "source_folder_path": str(src),
        "artist_name": "Checksum Test Act",
        "start_year": 2021, "start_month": 3, "start_day": 4,
        "source": "SBD",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}],
        "fingerprints": [{"type": "md5", "filename": "checksum.md5",
                          "rel_path": "checksum.md5", "path": str(src / "checksum.md5")}],
    }
    result = _do_confirm(data, uid, None)
    rec = _db.session.get(Recording, result["recording_id"])
    track = rec.tracks[0]

    assert rec.fingerprints[0].content == f"{real_md5} *t01.flac\n"
    assert track.checksum_type == "md5"
    assert track.expected_checksum == real_md5
    assert track.checksum_status == "match"
    assert track.checksum_verified_at is not None


def test_do_confirm_flags_checksum_mismatch(app, db, tmp_path):
    from app.api.ingest import _do_confirm
    from app.models.user import User

    src = tmp_path / "src_show3"; src.mkdir()
    (src / "t01.flac").write_bytes(b"some audio bytes")
    (src / "checksum.md5").write_text("0" * 32 + " *t01.flac\n")  # deliberately wrong
    lib = tmp_path / "lib3"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    data = {
        "source_folder_path": str(src),
        "artist_name": "Checksum Mismatch Act",
        "start_year": 2021, "start_month": 3, "start_day": 4,
        "source": "SBD",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}],
        "fingerprints": [{"type": "md5", "filename": "checksum.md5",
                          "rel_path": "checksum.md5", "path": str(src / "checksum.md5")}],
    }
    result = _do_confirm(data, uid, None)
    rec = _db.session.get(Recording, result["recording_id"])
    assert rec.tracks[0].checksum_status == "mismatch"


def test_verify_checksums_discovers_and_verifies_backfill(app, db, api, tmp_path):
    """A fingerprint file that wasn't sent at confirm time gets discovered
    from the library folder, parsed, and verified by the endpoint alone —
    covers Ryan's 'go back and re-process' ask, 2026-07-13.

    The checksum file is written against the track's post-ingest filename
    (its stored file_path) rather than the pre-ingest one — a real backfill
    checksum file is generated by re-running shntool/flac against what's
    actually sitting in the library folder today, and since 2026-07-14 that's
    always the flattened+renamed name (see compute_audio_rename_map), not
    whatever the original source folder called it."""
    import hashlib
    from app.api.ingest import _do_confirm
    from app.models.user import User

    src = tmp_path / "src_show4"; src.mkdir()
    audio_bytes = b"more fake flac bytes" * 10
    (src / "t01.flac").write_bytes(audio_bytes)
    lib = tmp_path / "lib4"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    data = {
        "source_folder_path": str(src),
        "artist_name": "Backfill Test Act",
        "start_year": 2022, "start_month": 5, "start_day": 6,
        "source": "AUD",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}],
        # deliberately no "fingerprints" — simulates a checksum file the
        # archivist forgot to include (or generated afterward) at confirm time
    }
    result = _do_confirm(data, uid, None)
    rec = _db.session.get(Recording, result["recording_id"])
    assert rec.tracks[0].checksum_status is None   # nothing to verify yet
    assert rec.tracks[0].file_path == "01 - One.flac"   # flattened + renamed on ingest

    # Drop a checksum file straight into the library folder, generated
    # against what's actually there now (the renamed filename).
    real_md5 = hashlib.md5(audio_bytes).hexdigest()
    (lib / rec.folder_path / "checksum.md5").write_text(f"{real_md5} *01 - One.flac\n")

    resp = api.post(f"/api/recordings/{rec.id}/verify-checksums")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["checked"] == 1
    assert body["tracks"][0]["checksum_status"] == "match"

    # Re-fetch from DB to confirm it actually persisted, not just the response.
    _db.session.refresh(rec.tracks[0])
    assert rec.tracks[0].checksum_type == "md5"


def test_save_info_file_overwrites_existing(api, tmp_path):
    """Editing the info file on the Add Recording form and saving it should
    write straight back to the real file scan_folder() found — independent
    of Confirm, per Ryan's 2026-07-13 ask (so a corrected file can drive a
    fresh AI Assist run before the show is ever added)."""
    src = tmp_path / "src_show5"; src.mkdir()
    (src / "info.txt").write_text("Original content\n")

    resp = api.post("/api/ingest/save-info-file", json={
        "folder_path": str(src), "filename": "info.txt", "content": "Corrected content\n",
    })
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert (src / "info.txt").read_text() == "Corrected content\n"


def test_save_info_file_creates_new_when_none_exists(api, tmp_path):
    """Folder had no info file — the archivist typed one in from scratch."""
    src = tmp_path / "src_show6"; src.mkdir()

    resp = api.post("/api/ingest/save-info-file", json={
        "folder_path": str(src), "filename": "info.txt", "content": "Typed from scratch\n",
    })
    assert resp.status_code == 200
    assert (src / "info.txt").read_text() == "Typed from scratch\n"


def test_save_info_file_rejects_path_traversal(api, tmp_path):
    src = tmp_path / "src_show7"; src.mkdir()
    outside = tmp_path / "escaped.txt"

    resp = api.post("/api/ingest/save-info-file", json={
        "folder_path": str(src), "filename": "../escaped.txt", "content": "nope",
    })
    assert resp.status_code == 400
    assert not outside.exists()


def test_artist_and_venue_delete_when_empty(api):
    pid = api.post("/api/artists/", json={"name": "Temp Act"}).get_json()["id"]
    assert api.delete(f"/api/artists/{pid}").status_code == 200
    assert _db.session.get(Artist, pid) is None

    vid = api.post("/api/venues/", json={"name": "Temp Hall"}).get_json()["id"]
    assert api.delete(f"/api/venues/{vid}").status_code == 200
    assert _db.session.get(Venue, vid) is None


# ── Multi-disc flatten + rename (2026-07-14) ────────────────────────────────
# Ryan's report: a CD1/CD2 source ingested with duplicate/interleaved track
# numbers (01,01,02,02,03... then 06-10 unduplicated) because each disc's
# FLAC files independently reset TRACKNUMBER. Root cause: multi-set/disc
# detection existed in scan_folder() but was fully dead code. Fix: wire up
# detection (below) + always flatten+rename audio into the library folder
# root on ingest (compute_audio_rename_map / move_to_library) so DB
# track_number is driven by the scan's own continuous index, never a
# per-disc tag that can collide.

def test_scan_folder_detects_and_orders_multi_disc(tmp_path):
    """CD1 (3 tracks) + CD2 (2 tracks), plus a non-set Art/ dir and a loose
    info file — sets_detected True, audio_files in continuous CD1-then-CD2
    order with correct 'set_number' labels and rel_path subdir prefixes intact
    (rel_path is only used to locate the ORIGINAL file pre-flatten)."""
    root = tmp_path / "multidisc_src"; root.mkdir()
    cd1 = root / "CD1"; cd1.mkdir()
    cd2 = root / "CD2"; cd2.mkdir()
    art = root / "Art"; art.mkdir()
    for n in (1, 2, 3):
        (cd1 / f"{n:02d}.flac").write_bytes(b"x")
    for n in (1, 2):
        (cd2 / f"{n:02d}.flac").write_bytes(b"x")
    (art / "cover.jpg").write_bytes(b"y")
    (root / "info.txt").write_text("Some show notes\n")

    result = scan_folder(str(root))

    assert result["sets_detected"] is True
    assert len(result["audio_files"]) == 5
    indices = [f["index"] for f in result["audio_files"]]
    assert indices == [1, 2, 3, 4, 5]                     # continuous, no reset
    assert [f["set_number"] for f in result["audio_files"]] == ["CD 1", "CD 1", "CD 1", "CD 2", "CD 2"]
    assert result["audio_files"][0]["rel_path"] in ("CD1/01.flac", "CD1\\01.flac")
    assert result["audio_files"][3]["rel_path"] in ("CD2/01.flac", "CD2\\01.flac")
    assert [o["filename"] for o in result["other_files"]] == ["cover.jpg"]
    assert [t["filename"] for t in result["text_files"]] == ["info.txt"]


def test_scan_folder_single_disc_subdir_not_flagged_as_set(tmp_path):
    """A single 'CD1' folder alone isn't multi-anything (nothing to flatten
    against) — sets_detected should stay False so a normal single-disc show
    with one 'flac/'-style subdir isn't needlessly treated as a multi-set."""
    root = tmp_path / "single_disc_src"; root.mkdir()
    cd1 = root / "CD1"; cd1.mkdir()
    (cd1 / "01.flac").write_bytes(b"x")

    result = scan_folder(str(root))
    assert result["sets_detected"] is False


# ── Spelled-out disc folders + multi-disc show resolution (2026-08-12) ──────
# Ryan's report: `del01-09-22` (disc one / disc two) came into the triage queue
# as TWO recordings. Two causes, one folder: the set pattern required DIGITS so
# "disc one" matched nothing, and resolve_shows() expanded any parent with 2+
# audio-bearing subdirs into separate shows. A multi-disc show is one show.

def test_parse_set_dir_word_and_digit_forms():
    """Both spellings resolve to the same canonical label, and the two
    deliberate exclusions hold: a bare 'd' prefix never takes a word number
    (or the staging folder 'done' becomes 'Disc 1'), and a prefix with no
    number at all is not a set."""
    from app.utils.ingest import _parse_set_dir
    assert _parse_set_dir("disc one")  == ("Disc 1", 1)
    assert _parse_set_dir("Disc Two")  == ("Disc 2", 2)
    assert _parse_set_dir("Set_Three") == ("Set 3", 3)
    assert _parse_set_dir("vol two")   == ("Vol 2", 2)
    assert _parse_set_dir("cd1")       == ("CD 1", 1)
    assert _parse_set_dir("CD 02")     == ("CD 2", 2)   # zero-pad normalised
    assert _parse_set_dir("d-3")       == ("Disc 3", 3)
    for junk in ("done", "disc", "setlist", "Artwork", "dm1996-05-31d1", ""):
        assert _parse_set_dir(junk) is None, junk


def test_resolve_shows_treats_multi_disc_folder_as_one_show(tmp_path):
    """A parent whose audio-bearing subdirs are named for discs resolves to
    ITSELF — one show — while a genuine grouping folder (dated subdirs) still
    expands. This is the triage-queue half of the bug."""
    from app.utils.ingest import resolve_shows, resolve_shows_in_dir
    imp = tmp_path / "import"; imp.mkdir()

    show = imp / "del01-09-22"; show.mkdir()
    d1 = show / "disc one"; d1.mkdir()
    d2 = show / "disc two"; d2.mkdir()
    for n in range(1, 19):
        (d1 / f"del01-09-22d1t{n:02d}.flac").write_bytes(b"x")
    for n in range(1, 9):
        (d2 / f"del01-09-22d2t{n:02d}.flac").write_bytes(b"x")

    grouping = imp / "Some Artist"; grouping.mkdir()
    for day in ("1977-05-08", "1977-05-09"):
        sub = grouping / day; sub.mkdir()
        (sub / "01.flac").write_bytes(b"x")

    assert resolve_shows(str(show)) == [str(show)]
    resolved = resolve_shows_in_dir(str(imp))
    assert resolved == [str(show), str(grouping / "1977-05-08"),
                        str(grouping / "1977-05-09")]


def test_scan_folder_spelled_out_disc_subdirs(tmp_path):
    """The ingest-scanner half: 'disc one'/'disc two' are sets, so the 26
    tracks land as ONE recording numbered 1..26 with disc two starting at 19,
    and each disc's own .md5 is still picked up for per-disc scoping."""
    show = tmp_path / "del01-09-22"; show.mkdir()
    d1 = show / "disc one"; d1.mkdir()
    d2 = show / "disc two"; d2.mkdir()
    for n in range(1, 19):
        (d1 / f"del01-09-22d1t{n:02d}.flac").write_bytes(b"x")
    for n in range(1, 9):
        (d2 / f"del01-09-22d2t{n:02d}.flac").write_bytes(b"x")
    (d1 / "del01-09-22d1.md5").write_text("x\n")
    (d2 / "del01-09-22d2.md5").write_text("x\n")

    result = scan_folder(str(show))

    assert result["sets_detected"] is True
    assert len(result["audio_files"]) == 26
    assert [f["index"] for f in result["audio_files"]] == list(range(1, 27))
    assert [f["set_number"] for f in result["audio_files"]] == ["Disc 1"] * 18 + ["Disc 2"] * 8
    d2_first = next(f for f in result["audio_files"] if "d2t01" in f["filename"])
    assert d2_first["index"] == 19
    assert sorted(f["filename"] for f in result["fingerprints"]) == [
        "del01-09-22d1.md5", "del01-09-22d2.md5"]


# ── Filename-encoded discs (2026-08-12) ─────────────────────────────────────
# Ryan's report: Pat Metheny 1979-06-14 ingested with duplicate track numbers
# (01,01,02,02...). Source was FLAT — no CD1/CD2 subdirs — with the disc in the
# FILENAME (D01T01..D01T09, D02T01..D02T07). Subdir-only set detection saw one
# folder, sets_detected stayed False, and the wizard fell back to trusting each
# file's TRACKNUMBER tag, which resets per disc. Same bug as 2026-07-14, with
# the disc number carried by the filename instead of a directory.

def test_scan_folder_detects_disc_prefix_in_filenames(tmp_path):
    """Flat folder, disc encoded as 'D01T01.' — sets_detected True, labels
    stamped, and index continuous across discs in (disc, track) order rather
    than in whatever order the filesystem listed them."""
    root = tmp_path / "flat_dtprefix"; root.mkdir()
    for n in range(1, 10):
        (root / f"D01T{n:02d}. Band - Song {n}.flac").write_bytes(b"x")
    for n in range(1, 8):
        (root / f"D02T{n:02d}. Band - Other {n}.flac").write_bytes(b"x")
    (root / "info.txt").write_text("Some show notes\n")

    result = scan_folder(str(root))

    assert result["sets_detected"] is True
    assert len(result["audio_files"]) == 16
    assert [f["index"] for f in result["audio_files"]] == list(range(1, 17))
    assert [f["set_number"] for f in result["audio_files"]] == ["Disc 1"] * 9 + ["Disc 2"] * 7
    # Disc 2 track 1 must land at index 10, not interleaved with disc 1's track 1
    assert result["audio_files"][9]["filename"].startswith("D02T01")
    assert [t["filename"] for t in result["text_files"]] == ["info.txt"]


def test_scan_folder_filename_disc_variants_and_ordering(tmp_path):
    """cd1t05 / CD2-03 style prefixes resolve to the same label vocabulary as
    subdir detection ('CD 1'), and numeric ordering beats lexical ordering
    (t10 after t2, which a plain filename sort would get wrong)."""
    root = tmp_path / "flat_cd"; root.mkdir()
    for n in (1, 2, 10):
        (root / f"cd1t{n}.flac").write_bytes(b"x")
    (root / "CD2-03.flac").write_bytes(b"x")

    result = scan_folder(str(root))

    assert result["sets_detected"] is True
    assert [f["filename"] for f in result["audio_files"]] == [
        "cd1t1.flac", "cd1t2.flac", "cd1t10.flac", "CD2-03.flac"]
    assert [f["set_number"] for f in result["audio_files"]] == ["CD 1"] * 3 + ["CD 2"]


def test_scan_folder_filename_disc_requires_all_files_and_two_discs(tmp_path):
    """Two guards, both no-ops rather than half-labelled sets: a single disc
    isn't multi-anything, and one unprefixed file means the convention isn't
    really in use (a bonus track shouldn't turn the rest into 'Disc 1')."""
    single = tmp_path / "one_disc"; single.mkdir()
    for n in (1, 2, 3):
        (single / f"d01t{n:02d}.flac").write_bytes(b"x")
    r1 = scan_folder(str(single))
    assert r1["sets_detected"] is False
    assert all(f["set_number"] is None for f in r1["audio_files"])

    mixed = tmp_path / "mixed"; mixed.mkdir()
    (mixed / "d01t01.flac").write_bytes(b"x")
    (mixed / "d02t01.flac").write_bytes(b"x")
    (mixed / "bonus encore.flac").write_bytes(b"x")
    r2 = scan_folder(str(mixed))
    assert r2["sets_detected"] is False


def test_scan_folder_ordinary_filenames_are_not_disc_prefixes(tmp_path):
    """Regression guard on the regex: a normal single-disc show numbered
    01/02/03 must not be read as disc-prefixed, or every flat recording in
    the library would sprout set labels."""
    root = tmp_path / "normal"; root.mkdir()
    for n in (1, 2, 3):
        (root / f"{n:02d} - Song {n}.flac").write_bytes(b"x")
    result = scan_folder(str(root))
    assert result["sets_detected"] is False
    assert [f["index"] for f in result["audio_files"]] == [1, 2, 3]


def test_scan_folder_subdirs_win_over_filename_prefixes(tmp_path):
    """When BOTH signals are present, the subdir pass owns the result — the
    filename pass is explicitly a fallback and must not renumber a list that
    subdir detection already ordered."""
    root = tmp_path / "both"; root.mkdir()
    cd1 = root / "CD1"; cd1.mkdir()
    cd2 = root / "CD2"; cd2.mkdir()
    (cd1 / "d01t01.flac").write_bytes(b"x")
    (cd1 / "d01t02.flac").write_bytes(b"x")
    (cd2 / "d02t01.flac").write_bytes(b"x")

    result = scan_folder(str(root))
    assert result["sets_detected"] is True
    assert [f["set_number"] for f in result["audio_files"]] == ["CD 1", "CD 1", "CD 2"]
    assert [f["index"] for f in result["audio_files"]] == [1, 2, 3]


def test_compute_audio_rename_map_pads_and_dedupes():
    """Zero-padding scales to the highest track number (2-digit minimum),
    and a naming collision (two tracks that would produce the same flat
    filename) gets a distinguishing suffix rather than one silently
    overwriting the other on disk."""
    tracks = [
        {"track_number": 1,  "title": "Improv",           "filename": "CD1/01.flac"},
        {"track_number": 2,  "title": "Improv",           "filename": "CD1/02.flac"},  # collides with #1
        {"track_number": 11, "title": "Set Break Jam",    "filename": "CD2/03.flac"},
    ]
    m = compute_audio_rename_map(tracks)

    assert m["CD1/01.flac"] == "01 - Improv.flac"
    assert m["CD1/02.flac"] == "02 - Improv.flac"     # track number, not title, disambiguates
    assert m["CD2/03.flac"] == "11 - Set Break Jam.flac"
    assert len(set(m.values())) == 3                  # no two tracks share a filename

    # True same-number collision (defensive case — shouldn't happen from a
    # real scan, but the map must never drop a track silently).
    dup_num = [
        {"track_number": 1, "title": "Jam", "filename": "a.flac"},
        {"track_number": 1, "title": "Jam", "filename": "b.flac"},
    ]
    m2 = compute_audio_rename_map(dup_num)
    assert m2["a.flac"] == "01 - Jam.flac"
    assert m2["b.flac"] == "01 - Jam (2).flac"


def test_compute_audio_rename_map_sanitizes_illegal_characters():
    tracks = [{"track_number": 1, "title": 'Dark Star -> Truckin\' / "Live"',
               "filename": "01.flac"}]
    m = compute_audio_rename_map(tracks)
    name = m["01.flac"]
    assert "/" not in name and ":" not in name and '"' not in name
    assert name.startswith("01 - ")


def test_do_confirm_flattens_and_renames_multi_disc_with_checksums(app, db, tmp_path):
    """End-to-end: a CD1/CD2 source, each disc with its OWN checksum file
    listing bare local filenames ("01.flac", "02.flac", ...) as most
    real-world per-disc checksum files do. Confirms audio flattens to the
    recording folder root with continuous cross-disc numbering, non-audio
    content (the checksum files themselves) keeps its original nested
    location, and checksums still correctly match + verify despite the
    rename — proving both the confirm-time proxy match
    (app.api.ingest._ChecksumMatchProxy) AND the per-fingerprint-file
    directory scoping (needed because "01.flac" is not unique across discs
    once matching falls back to original names) work end to end."""
    import hashlib
    from app.api.ingest import _do_confirm
    from app.models.user import User

    src = tmp_path / "src_multidisc"; src.mkdir()
    cd1 = src / "CD1"; cd1.mkdir()
    cd2 = src / "CD2"; cd2.mkdir()

    b1 = b"disc one track one audio bytes" * 5
    b2 = b"disc one track two audio bytes" * 5
    b3 = b"disc two track one audio bytes" * 5     # same local name "01.flac" as b1
    (cd1 / "01.flac").write_bytes(b1)
    (cd1 / "02.flac").write_bytes(b2)
    (cd2 / "01.flac").write_bytes(b3)

    # Each disc ships its OWN checksum file, scoped to its own bare
    # filenames — CD1's "01.flac" and CD2's "01.flac" are unrelated files
    # with unrelated hashes, exactly the ambiguity flattening creates if
    # matching isn't scoped per fingerprint file's original directory.
    md5_1, md5_2, md5_3 = (hashlib.md5(b).hexdigest() for b in (b1, b2, b3))
    (cd1 / "checksum.md5").write_text(f"{md5_1} *01.flac\n{md5_2} *02.flac\n")
    (cd2 / "checksum.md5").write_text(f"{md5_3} *01.flac\n")

    lib = tmp_path / "lib_multidisc"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id

    scan = scan_folder(str(src))
    assert scan["sets_detected"] is True
    assert len(scan["fingerprints"]) == 2   # both discs' checksum files found

    tracks_payload = [
        {"track_number": i + 1, "title": f"Track {i + 1}",
         "duration": 100, "filename": af["rel_path"], "set_number": af["set_number"]}
        for i, af in enumerate(scan["audio_files"])
    ]
    data = {
        "source_folder_path": str(src),
        "artist_name": "Multi Disc Test Trio",
        "start_year": 1979, "start_month": 12, "start_day": 12,
        "source": "SBD",
        "tracks": tracks_payload,
        "fingerprints": [
            {"type": fp["type"], "filename": fp["filename"], "rel_path": fp["rel_path"]}
            for fp in scan["fingerprints"]
        ],
    }
    result = _do_confirm(data, uid, None)
    rec = _db.session.get(Recording, result["recording_id"])
    tracks = sorted(rec.tracks, key=lambda t: t.track_number)

    # Continuous numbering across discs, no reset/duplication.
    assert [t.track_number for t in tracks] == [1, 2, 3]
    # Flattened: no subdir prefix in the stored path, and files actually
    # live at the folder root on disk — even though the checksum files that
    # shipped alongside them (non-audio) keep their original CD1/CD2 nesting.
    assert [t.file_path for t in tracks] == [
        "01 - Track 1.flac", "02 - Track 2.flac", "03 - Track 3.flac",
    ]
    for t in tracks:
        assert (lib / rec.folder_path / t.file_path).exists()
    assert not (lib / rec.folder_path / "CD1" / "01.flac").exists()   # audio moved out
    assert (lib / rec.folder_path / "CD1" / "checksum.md5").exists()  # non-audio stayed put
    assert (lib / rec.folder_path / "CD2" / "checksum.md5").exists()

    # Checksums matched against the ORIGINAL filenames each disc's own
    # fingerprint file listed, scoped so CD2's "01.flac" never gets matched
    # to CD1's track of the same original name, and verified against the
    # real (renamed) audio bytes.
    assert [t.checksum_status for t in tracks] == ["match", "match", "match"]
    assert tracks[0].expected_checksum == md5_1   # CD1/01.flac → Track 1
    assert tracks[1].expected_checksum == md5_2   # CD1/02.flac → Track 2
    assert tracks[2].expected_checksum == md5_3   # CD2/01.flac → Track 3 (not CD1's md5_1)
    assert tracks[2].expected_checksum == md5_3


# ── Duplicate detection across artist/musician variants (2026-08-02) ─────────
# The triage card now warns when the library already holds this act on this
# date. Exact-name matching alone would miss the variant that actually bites:
# "Aoife O'Donovan" and "Aoife O'Donovan Band" are one act to a collector and
# two Artist rows in the DB.

def test_act_key_strips_decoration_but_keeps_identity():
    from app.api.ingest import _act_key
    assert _act_key("Aoife O'Donovan Band") == _act_key("Aoife O'Donovan")
    assert _act_key("The Allman Brothers Band") == _act_key("Allman Brothers")
    assert _act_key("Bela Fleck Trio") == _act_key("Bela Fleck")
    # Distinct acts must NOT collapse — a false duplicate warning on every card
    # would train the eye to ignore the one that matters.
    assert _act_key("Bela Fleck") != _act_key("Jerry Garcia")


def test_act_key_never_returns_empty_for_an_all_noise_name():
    """"The Band" is entirely noise words. Falling through to the raw words is
    the safe answer — returning "" would make it match every other act."""
    from app.api.ingest import _act_key
    assert _act_key("The Band") == "the band"


def test_resolve_similar_artist_ids_finds_variants(app):
    from app.extensions import db as _db
    from app.models.artist import Artist
    from app.api.ingest import resolve_similar_artist_ids

    with app.app_context():
        exact   = Artist(name="Aoife O'Donovan")
        variant = Artist(name="Aoife O'Donovan Band")
        typo    = Artist(name="Aoife ODonovan")
        other   = Artist(name="Punch Brothers")
        _db.session.add_all([exact, variant, typo, other])
        _db.session.commit()

        ids = set(resolve_similar_artist_ids("Aoife O'Donovan"))
        assert {exact.id, variant.id, typo.id} <= ids
        assert other.id not in ids


def test_resolve_similar_artist_ids_reaches_through_a_person(app):
    """Ryan asked for 'artist OR musician'. Since the 07-11 remodel those are
    different tables, so a person's name has to find the acts they play in."""
    from app.extensions import db as _db
    from app.models.artist import Artist
    from app.models.musician import Musician, Membership
    from app.api.ingest import resolve_similar_artist_ids

    with app.app_context():
        act    = Artist(name="Old And In The Way")
        person = Musician(name="Jerry Garcia")
        _db.session.add_all([act, person])
        _db.session.flush()
        _db.session.add(Membership(artist_id=act.id, musician_id=person.id))
        _db.session.commit()

        assert act.id in resolve_similar_artist_ids("Jerry Garcia")


def test_resolve_similar_artist_ids_is_empty_for_a_blank_name():
    from app.api.ingest import resolve_similar_artist_ids
    assert resolve_similar_artist_ids("") == []
    assert resolve_similar_artist_ids(None) == []


# ── Genre (2026-08-02) ───────────────────────────────────────────────────────
# Genre is a proper dimension: its own table, one nullable FK from Artist,
# guarded delete matching Venue/Collection/Musician. See the Genre design spec
# in Context Library. Nothing may create a genre implicitly — every picker
# selects from the existing table only.

def test_genre_name_uniqueness(api):
    r1 = api.post("/api/genres/", json={"name": "Ska"})
    assert r1.status_code == 201
    # Exact duplicate
    r2 = api.post("/api/genres/", json={"name": "Ska"})
    assert r2.status_code == 409
    # Case-insensitive duplicate
    r3 = api.post("/api/genres/", json={"name": "ska"})
    assert r3.status_code == 409

    # Renaming an existing genre to collide with another must also be refused.
    other = api.post("/api/genres/", json={"name": "Zydeco"}).get_json()
    dup = api.put(f"/api/genres/{other['id']}", json={"name": "ska"})
    assert dup.status_code == 409


def test_genre_color_roundtrip_validation_and_clearing(api):
    """Colour is `#rrggbb` only, normalised to lowercase, and CLEARABLE.

    Clearing matters as much as setting: NULL is a first-class state that
    renders the same neutral grey as a artist with no genre at all, so it
    must not be a validation failure. Shorthand and named colours are refused
    at the door so every consumer gets one canonical form.
    """
    g = api.post("/api/genres/", json={"name": "Dub", "color": "#AABBCC"}).get_json()
    assert g["color"] == "#aabbcc"          # normalised on create

    assert api.put(f"/api/genres/{g['id']}", json={"color": "#123abc"}).status_code == 200
    assert api.get(f"/api/genres/{g['id']}").get_json()["color"] == "#123abc"

    for bad in ("#abc", "red", "123abc", "#12345g"):
        r = api.put(f"/api/genres/{g['id']}", json={"color": bad})
        assert r.status_code == 400, bad

    # Both null and empty string clear it.
    assert api.put(f"/api/genres/{g['id']}", json={"color": None}).status_code == 200
    assert api.get(f"/api/genres/{g['id']}").get_json()["color"] is None
    api.put(f"/api/genres/{g['id']}", json={"color": "#aabbcc"})
    assert api.put(f"/api/genres/{g['id']}", json={"color": ""}).status_code == 200
    assert api.get(f"/api/genres/{g['id']}").get_json()["color"] is None


def test_recording_row_card_fields_opt_in(app, seeded_ids):
    """`card=True` adds genre/genre_color/image_id; default omits them entirely.

    Absence, not null, is the assertion for the default case — this serializer
    also backs the 544-row flat List, and each card field walks Recording →
    Performance → Artist → (Genre | ArtistImage). Shipping them
    unconditionally would tax List to benefit two small Browse modules.
    """
    from app.extensions import db as _db
    from app.utils.serialize import recording_row
    from app.models.recording import Recording
    from app.models.genre import Genre
    from app.models.artist_image import ArtistImage

    rec = _db.session.get(Recording, seeded_ids["recording_id"])

    plain = recording_row(rec)
    for key in ("genre", "genre_color", "image_id"):
        assert key not in plain

    card = recording_row(rec, card=True)
    assert card["genre"] is None          # no genre assigned yet
    assert card["genre_color"] is None    # never substitutes a default
    assert card["image_id"] is None

    artist = rec.performance.artist
    artist.genre = Genre(name="Trip Hop", color="#445566")
    _db.session.add(ArtistImage(artist_id=artist.id,
                                   filename="img_x.jpg", ext=".jpg",
                                   is_primary=True))
    _db.session.commit()

    card = recording_row(rec, card=True)
    assert card["genre"] == "Trip Hop"
    assert card["genre_color"] == "#445566"
    assert card["image_id"] is not None


def test_recording_row_card_image_falls_back_when_no_primary_flagged(app, seeded_ids):
    """A artist with images but none flagged primary still gets a face.

    Deleting the primary must never leave a card blank while photos exist, so
    the serializer falls back to the first image rather than requiring the flag.
    """
    from app.extensions import db as _db
    from app.utils.serialize import recording_row
    from app.models.recording import Recording
    from app.models.artist_image import ArtistImage

    rec = _db.session.get(Recording, seeded_ids["recording_id"])
    pid = rec.performance.artist_id
    _db.session.add(ArtistImage(artist_id=pid, filename="img_y.png",
                                   ext=".png", is_primary=False))
    _db.session.commit()

    assert recording_row(rec, card=True)["image_id"] is not None


def test_genre_delete_guarded_while_referenced(api, seeded_ids):
    from app.models.artist import Artist

    g = api.post("/api/genres/", json={"name": "Test Guard Genre"}).get_json()
    pid = seeded_ids["artist_id"]
    upd = api.put(f"/api/artists/{pid}", json={"genre_id": g["id"]})
    assert upd.status_code == 200

    refused = api.delete(f"/api/genres/{g['id']}")
    assert refused.status_code == 409
    assert "1" in refused.get_json()["error"]
    assert _db.session.get(Artist, pid).genre_id == g["id"]   # untouched

    # Clear the assignment, then delete succeeds.
    api.put(f"/api/artists/{pid}", json={"genre_id": None})
    ok = api.delete(f"/api/genres/{g['id']}")
    assert ok.status_code == 200


def test_genre_delete_when_unreferenced(api):
    g = api.post("/api/genres/", json={"name": "Unreferenced Genre"}).get_json()
    assert api.delete(f"/api/genres/{g['id']}").status_code == 200


def test_artist_genre_nullable_and_defaults_null(api, seeded_ids):
    """A artist with no genre assignment (the seeded default) serializes
    genre: null, never errors — the FK is nullable by design (plenty of acts
    resist a single label)."""
    resp = api.get(f"/api/artists/{seeded_ids['artist_id']}")
    assert resp.status_code == 200
    assert resp.get_json()["genre"] is None


def test_artist_put_accepts_and_clears_genre_id(api, seeded_ids):
    pid = seeded_ids["artist_id"]
    g = api.post("/api/genres/", json={"name": "Newgrass Test"}).get_json()

    r = api.put(f"/api/artists/{pid}", json={"genre_id": g["id"]})
    assert r.status_code == 200
    fetched = api.get(f"/api/artists/{pid}").get_json()
    # Field-wise rather than whole-dict equality: the genre payload gained
    # `color` on 2026-08-07 and will gain more. This test is about the FK
    # assignment round-tripping, so it should not fail every time an unrelated
    # display field is added to the serializer.
    assert fetched["genre"]["id"] == g["id"]
    assert fetched["genre"]["name"] == "Newgrass Test"
    assert fetched["genre"]["color"] is None   # no colour set on create

    # Clearing back to null is a legitimate, supported edit.
    r2 = api.put(f"/api/artists/{pid}", json={"genre_id": None})
    assert r2.status_code == 200
    assert api.get(f"/api/artists/{pid}").get_json()["genre"] is None


def test_artist_put_rejects_unknown_genre_id(api, seeded_ids):
    pid = seeded_ids["artist_id"]
    r = api.put(f"/api/artists/{pid}", json={"genre_id": 999999})
    assert r.status_code == 400


def test_migrate_add_genre_idempotent_and_seeds_twenty(tmp_path):
    """Runs the migration script twice against a bare `performer` table (no
    genre table, no genre_id column yet) — first run creates the table,
    column, and seeds all 20 genres; second run is a pure no-op on every
    front (no duplicate table/column errors, no duplicate seed rows)."""
    import sqlite3
    from scripts import migrate_add_genre as mod

    db_path = tmp_path / "legacy.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE performer (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    con.commit()
    con.close()

    original_db = mod.DB
    try:
        mod.DB = str(db_path)
        mod.main()
        mod.main()   # idempotent — must not raise
    finally:
        mod.DB = original_db

    con = sqlite3.connect(str(db_path))
    cols = [r[1] for r in con.execute("PRAGMA table_info(performer)")]
    names = [r[0] for r in con.execute("SELECT name FROM genre")]
    con.close()

    assert "genre_id" in cols
    assert len(names) == 20
    assert len(set(names)) == 20   # no duplicates from the second run
    assert "Bluegrass" in names and "Jam" in names


# ── Library Browse View (2026-08-02 design spec) ────────────────────────────

def test_recording_row_omits_waveform_by_default(app, seeded_ids):
    """Load-bearing default: recording_row() also feeds the flat List views
    (recent, collection, venue) — the waveform key must be entirely ABSENT,
    not just None, so those endpoints never pay for TrackAnalysis JSON they
    don't use."""
    rec = _db.session.get(Recording, seeded_ids["recording_id"])
    row = recording_row(rec)
    assert "waveform" not in row


def test_recording_row_waveform_opt_in_uses_longest_analysed_track(app, seeded_ids):
    """Passing waveform=True adds the key, sourced from the longest ANALYSED
    track — not track 1. The seeded recording's t1 (300s) outlasts t2 (60s,
    'tuning'), so t1's peaks are the ones that should come back downsampled."""
    rec = _db.session.get(Recording, seeded_ids["recording_id"])
    t1, t2 = sorted(rec.tracks, key=lambda t: t.track_number)   # 300s, 60s

    # Give BOTH tracks real peaks so picking t1 is actually proving the
    # "longest" rule, not just "the only one with data".
    ta1 = _db.session.query(TrackAnalysis).filter_by(track_id=t1.id).first()
    ta1.waveform_json = _json.dumps([0.1, 0.9, 0.2, 0.9])
    _db.session.add(TrackAnalysis(track_id=t2.id, waveform_json=_json.dumps([0.5, 0.5])))
    _db.session.commit()

    row = recording_row(rec, waveform=True)
    assert "waveform" in row
    assert isinstance(row["waveform"], list)
    assert len(row["waveform"]) == 100
    assert max(row["waveform"]) > 0.8   # t1's 0.9 peaks, not t2's flat 0.5s


def test_recording_row_waveform_none_when_no_track_analysed(app, tmp_path):
    """A recording with no TrackAnalysis rows at all (the ~3% un-analysed
    share) must not error — waveform comes back None, and the frontend
    degrades to a flat strip rather than leaving a hole."""
    artist = Artist(name="No Analysis Act")
    _db.session.add(artist); _db.session.flush()
    perf = Performance(artist_id=artist.id, start_year=2020, start_month=1, start_day=1)
    _db.session.add(perf); _db.session.flush()
    rec = Recording(performance_id=perf.id, source="AUD", folder_path="x/no-analysis")
    _db.session.add(rec); _db.session.flush()
    _db.session.add(Track(recording_id=rec.id, track_number=1, title="One",
                          duration=100, file_path="01.flac"))
    _db.session.commit()

    row = recording_row(rec, waveform=True)
    assert row["waveform"] is None


def test_on_this_day_matches_month_and_day_regardless_of_year(api, db):
    """Matches on start_month/start_day only — any year. A show on a
    different month (same day-of-month) must NOT match."""
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date()
    other_month = (today.month % 12) + 1   # guaranteed different from today.month

    artist = Artist(name="On This Day Act")
    db.session.add(artist); db.session.flush()

    perf_match = Performance(artist_id=artist.id, start_year=1975,
                             start_month=today.month, start_day=today.day)
    db.session.add(perf_match); db.session.flush()
    rec_match = Recording(performance_id=perf_match.id, source="AUD", folder_path="x/match")
    db.session.add(rec_match); db.session.flush()
    db.session.add(Track(recording_id=rec_match.id, track_number=1, title="One",
                         duration=100, file_path="01.flac"))

    perf_miss = Performance(artist_id=artist.id, start_year=1980,
                            start_month=other_month, start_day=today.day)
    db.session.add(perf_miss); db.session.flush()
    rec_miss = Recording(performance_id=perf_miss.id, source="AUD", folder_path="x/miss")
    db.session.add(rec_miss); db.session.flush()
    db.session.add(Track(recording_id=rec_miss.id, track_number=1, title="One",
                         duration=100, file_path="01.flac"))
    db.session.commit()

    resp = api.get("/api/recordings/on-this-day")
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.get_json()]
    assert rec_match.id in ids
    assert rec_miss.id not in ids


def test_on_this_day_empty_when_nothing_matches(api, seeded_ids):
    """The seeded recording is dated 1980-02-22 — matches today only by
    coincidence. On any other day this must return an empty list, not error
    (the frontend hides the module entirely on an empty response)."""
    from datetime import datetime, timezone
    resp = api.get("/api/recordings/on-this-day")
    assert resp.status_code == 200
    today = datetime.now(timezone.utc).date()
    if (today.month, today.day) != (2, 22):
        assert resp.get_json() == []


def test_recommended_pool_is_a_and_a_plus_only_not_a_minus(api, db):
    """'High quality' means quality IN ('A','A+') — A- is deliberately
    excluded (Ryan, 2026-08-02)."""
    artist = Artist(name="Recommended Pool Act")
    db.session.add(artist); db.session.flush()

    def _make(quality, suffix):
        perf = Performance(artist_id=artist.id, start_year=2000, start_month=1, start_day=1)
        db.session.add(perf); db.session.flush()
        rec = Recording(performance_id=perf.id, source="SBD", quality=quality,
                        folder_path=f"x/{suffix}")
        db.session.add(rec); db.session.flush()
        db.session.add(Track(recording_id=rec.id, track_number=1, title="One",
                             duration=100, file_path="01.flac"))
        return rec

    a_plus = _make("A+", "aplus")
    a_minus = _make("A-", "aminus")
    db.session.commit()

    resp = api.get("/api/recordings/recommended?limit=50")
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.get_json()]
    assert a_plus.id in ids
    assert a_minus.id not in ids


def test_recommended_never_repeats_a_artist_in_one_draw(api, db):
    """Diversity is a hard rule: two A/A+ shows by the same Artist must
    never both appear in a single Recommended draw."""
    artist = Artist(name="Prolific Act")
    db.session.add(artist); db.session.flush()

    made = []
    for i in range(4):
        perf = Performance(artist_id=artist.id, start_year=2000 + i, start_month=1, start_day=1)
        db.session.add(perf); db.session.flush()
        rec = Recording(performance_id=perf.id, source="SBD", quality="A",
                        folder_path=f"x/prolific{i}")
        db.session.add(rec); db.session.flush()
        db.session.add(Track(recording_id=rec.id, track_number=1, title="One",
                             duration=100, file_path="01.flac"))
        made.append(rec)
    db.session.commit()

    resp = api.get("/api/recordings/recommended?limit=3")
    assert resp.status_code == 200
    picks = resp.get_json()
    ids = [r["id"] for r in picks]
    # All four candidates share one Artist — at most one of them may be
    # picked no matter how many slots are requested.
    assert len(set(ids) & {r.id for r in made}) <= 1
    assert len(ids) == len(set(ids))   # no recording picked twice


def test_recommended_omits_waveform_and_respects_limit(api, seeded_ids, db):
    """`limit` is honored, and the payload carries NO waveform.

    Inverted 2026-08-07 when the Browse card became a handbill rendered from
    metadata. Asserting the ABSENCE matters: the waveform opt-in triggers an
    eager load of every track's analysis row, so a future caller quietly
    switching it back on is a real performance regression, not a cosmetic one.
    The card's own fields are asserted here so the endpoint can't drop them.
    """
    rec = _db.session.get(Recording, seeded_ids["recording_id"])
    rec.quality = "A+"
    db.session.commit()

    resp = api.get("/api/recordings/recommended?limit=1")
    assert resp.status_code == 200
    picks = resp.get_json()
    assert len(picks) <= 1
    if picks:
        assert "waveform" not in picks[0]
        for key in ("artist", "start_year", "venue", "source"):
            assert key in picks[0]


def test_recommended_empty_pool_returns_empty_list(api, seeded_ids, db):
    """No A/A+ recordings in the library (the seeded recording is B+) —
    empty list, not an error. The frontend hides the module entirely."""
    resp = api.get("/api/recordings/recommended")
    assert resp.status_code == 200
    assert resp.get_json() == []
