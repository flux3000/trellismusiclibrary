"""
tests/test_resolve.py -- the Ingest Field Resolver (app/utils/resolve.py):
the Metheny regression, date/artist conflict handling, verdict()'s partial-
date reasons, track alignment across a restarting info-file listing, and
duplicate detection. Real tiny FLAC files tagged via mutagen, real folders
under tmp_path, same fixture style as tests/test_bulk_ingest_classify.py.
"""
import os

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from app.utils.ingest import build_scan_payload
from app.utils.resolve import resolve, resolve_tracks, verdict, classify_kind


def _flac_with_tags(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for k, v in tags.items():
        audio[k] = v
    audio.save()


def _ffp(path, hexhash, filename="01.flac"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{hexhash} *{filename}\n")


_METHENY_INFO = '''Pat Metheny Group
June 14, 1979
Stars – Philadelphia, Pennsylvania, USA
Two Shows: Show 1: 9:00 p.m. and Show 2: 11:00 p.m.
Soundboard Recording

Disc/Show 1 Lineage: "Unity Village" CD > EAC > FLAC

Show 1 (AKA "Unity Village")
01.  Phase Dance 7:49
02.  Circus 6:39
03.  Pat Talks 0:44

Show 2
01.  Mars 12:39
02.  Jaco 3:55
'''


def _metheny_folder(tmp_path):
    folder_name = "Pat Metheny Group - 1979-06-14 - Stars - Philadelphia, PA (SBD)"
    show = tmp_path / folder_name
    for i in range(1, 6):
        _flac_with_tags(show / f"{i:02d}.flac", ARTIST="Pat Metheny Group", DATE="1979")
    (show / "info.txt").write_text(_METHENY_INFO)
    return show


# ── Metheny regression (spec section 1) ─────────────────────────────────────

def test_metheny_regression_venue_and_date_and_verdict(tmp_path):
    show = _metheny_folder(tmp_path)
    scan = build_scan_payload(str(show))
    resolved = resolve(scan, library_root=str(tmp_path), placement="artist")

    assert resolved.date.value == {"year": 1979, "month": 6, "day": 14}
    assert resolved.date.source == "info"
    assert resolved.date.conflict is False

    assert resolved.venue.value == "Stars"
    assert resolved.city.value == "Philadelphia"
    assert resolved.state.value == "PA"

    status, reasons = verdict(resolved)
    assert status == "ingested"
    assert reasons == []


def test_metheny_tracks_align_by_position_across_the_show_2_restart(tmp_path):
    # The info file's own printed numbers restart at 01 for Show 2 -- this
    # is exactly the bug the position-alignment rule (spec 3.2) exists to
    # avoid: with 5 audio files and 5 info tracks, alignment is by
    # POSITION, so "Mars"/"Jaco" land on tracks 4 and 5, not colliding with
    # 1/2 again.
    show = _metheny_folder(tmp_path)
    scan = build_scan_payload(str(show))
    tracks = resolve_tracks(scan)
    assert [t["track_number"] for t in tracks] == [1, 2, 3, 4, 5]
    assert [t["title"] for t in tracks] == \
        ["Phase Dance", "Circus", "Pat Talks", "Mars", "Jaco"]


# ── Date: conflict / precision (spec 3.1, section 5) ────────────────────────

def test_date_conflict_when_tag_and_info_years_disagree(tmp_path):
    show = tmp_path / "show"
    _flac_with_tags(show / "01.flac", ARTIST="Test Band", DATE="1979-06-14")
    (show / "info.txt").write_text("Test Band\nJune 14, 1980\nSome Venue, City, NY\n")
    scan = build_scan_payload(str(show))
    resolved = resolve(scan)
    assert resolved.date.conflict is True
    status, reasons = verdict(resolved)
    assert status == "review"
    assert "conflict:date" in reasons


def test_date_agreement_when_one_source_is_just_more_precise(tmp_path):
    # "1979" (tags) vs "1979-06-14" (info) is agreement, not a conflict --
    # the more precise value wins.
    show = tmp_path / "show"
    _flac_with_tags(show / "01.flac", ARTIST="Test Band", DATE="1979")
    (show / "info.txt").write_text("Test Band\nJune 14, 1979\nSome Venue, City, NY\n")
    scan = build_scan_payload(str(show))
    resolved = resolve(scan)
    assert resolved.date.conflict is False
    assert resolved.date.value == {"year": 1979, "month": 6, "day": 14}


def test_year_and_month_needs_day(tmp_path):
    show = tmp_path / "show"
    _flac_with_tags(show / "01.flac", ARTIST="Test Band", DATE="1979-06")
    scan = build_scan_payload(str(show))
    resolved = resolve(scan)
    status, reasons = verdict(resolved)
    assert status == "review"
    assert reasons == ["needs_day"]


def test_year_only_needs_month(tmp_path):
    show = tmp_path / "show"
    _flac_with_tags(show / "01.flac", ARTIST="Test Band", DATE="1979")
    scan = build_scan_payload(str(show))
    resolved = resolve(scan)
    status, reasons = verdict(resolved)
    assert status == "review"
    assert reasons == ["needs_month"]


def test_no_date_at_all_needs_date(tmp_path):
    show = tmp_path / "show"
    _flac_with_tags(show / "01.flac", ARTIST="Test Band")
    scan = build_scan_payload(str(show))
    resolved = resolve(scan)
    status, reasons = verdict(resolved)
    assert status == "review"
    assert "needs_date" in reasons


def test_no_artist_needs_artist(tmp_path):
    show = tmp_path / "show"
    _flac_with_tags(show / "01.flac", DATE="1979-06-14")
    scan = build_scan_payload(str(show))
    resolved = resolve(scan)
    status, reasons = verdict(resolved)
    assert status == "review"
    assert "needs_artist" in reasons


def test_studio_album_ingests_without_any_date(tmp_path):
    show = tmp_path / "show"
    _flac_with_tags(show / "01.flac", ARTIST="Test Band", ALBUM="Studio LP")
    _flac_with_tags(show / "02.flac", ARTIST="Test Band", ALBUM="Studio LP")
    scan = build_scan_payload(str(show))
    resolved = resolve(scan)
    assert resolved.kind == "studio"
    status, reasons = verdict(resolved)
    assert status == "ingested"
    assert reasons == []


# ── Venue plausibility / dash split (spec section 7) ────────────────────────

def test_implausible_venue_line_is_treated_as_none(tmp_path):
    show = tmp_path / "show"
    _flac_with_tags(show / "01.flac", ARTIST="Test Band", DATE="1979-06-14")
    (show / "info.txt").write_text(
        "Test Band\nJune 14, 1979\nTwo Shows: Show 1: 9:00 p.m. and Show 2: 11:00 p.m.\n")
    scan = build_scan_payload(str(show))
    resolved = resolve(scan)
    assert resolved.venue.value is None


def test_placeholder_venue_counts_as_none(tmp_path):
    show = tmp_path / "show"
    _flac_with_tags(show / "01.flac", ARTIST="Test Band", DATE="1979-06-14",
                    VENUE="Unknown Venue")
    scan = build_scan_payload(str(show))
    resolved = resolve(scan)
    assert resolved.venue.value is None
    # Live show, full date, no venue -- venue is not required (unchanged).
    status, reasons = verdict(resolved)
    assert status == "ingested"


# ── Duplicates (spec section 6, section 9) ──────────────────────────────────

def _seed_recording(app, tmp_path, folder_rel, hexhash):
    """Ingest one tiny recording via the real confirm path, with an FFP
    fingerprint, so find_duplicates() has something real to match against."""
    from app.extensions import db as _db
    from app.models.user import User
    from app.api.ingest import _do_confirm

    show = tmp_path / folder_rel
    _flac_with_tags(show / "01.flac", ARTIST="Dup Band", DATE="1979-06-14")
    _ffp(show / "checksum.ffp", hexhash)
    user = _db.session.query(User).first()
    scan = build_scan_payload(str(show))
    resolved = resolve(scan)
    payload = {
        "source_folder_path": str(show),
        "artist_name": resolved.artist.value,
        "start_year": resolved.date.value["year"],
        "start_month": resolved.date.value["month"],
        "start_day": resolved.date.value["day"],
        "fingerprints": scan.get("fingerprints"),
        "tracks": resolved.tracks,
        "is_complete": True,
        "skip_analysis": True,
    }
    result = _do_confirm(payload, user.id)
    return result["recording_id"]


_DUP_HASH = "abcd1234abcd1234abcd1234abcd1234"


def test_exact_content_duplicate_is_skipped(app, tmp_path):
    from app.utils.resolve import find_duplicates

    app.config["LIBRARY_ROOT"] = str(tmp_path)
    existing_id = _seed_recording(app, tmp_path, "Original", _DUP_HASH)

    show = tmp_path / "Copy"
    _flac_with_tags(show / "01.flac", ARTIST="Dup Band", DATE="1979-06-14")
    _ffp(show / "checksum.ffp", _DUP_HASH)
    scan = build_scan_payload(str(show))
    resolved = resolve(scan)
    resolved.duplicates = find_duplicates(resolved, library_root=str(tmp_path))
    status, reasons = verdict(resolved)
    assert status == "skipped"
    assert reasons == ["duplicate_content"]
    dup = next(d for d in resolved.duplicates if d.kind == "content")
    assert dup.recording_id == existing_id


def test_partial_content_overlap_is_not_a_duplicate(app, tmp_path):
    from app.utils.resolve import find_duplicates

    app.config["LIBRARY_ROOT"] = str(tmp_path)
    _seed_recording(app, tmp_path, "Original", _DUP_HASH)

    # A second folder that shares ONE hash with the existing recording but
    # also has a track the existing one doesn't -- partial overlap, not an
    # exact duplicate, so it must ingest normally.
    show = tmp_path / "PartialOverlap"
    _flac_with_tags(show / "01.flac", ARTIST="Dup Band", DATE="1979-06-14")
    _flac_with_tags(show / "02.flac", ARTIST="Dup Band", DATE="1979-06-14")
    (show / "checksum.ffp").write_text(
        f"{_DUP_HASH} *01.flac\nffffffffffffffffffffffffffffffff *02.flac\n")
    scan = build_scan_payload(str(show))
    resolved = resolve(scan)
    resolved.duplicates = find_duplicates(resolved, library_root=str(tmp_path))
    assert not any(d.kind == "content" for d in resolved.duplicates)
    status, reasons = verdict(resolved)
    assert status == "ingested"


# ── classify_kind moved unchanged (spec 3.1) ────────────────────────────────

def test_classify_kind_studio_vs_live(tmp_path):
    show_live = tmp_path / "live"
    _flac_with_tags(show_live / "01.flac", ARTIST="Test Band", DATE="1979-06-14")
    scan_live = build_scan_payload(str(show_live))
    assert classify_kind(scan_live, None, "Some Venue") == "live"

    show_studio = tmp_path / "studio"
    _flac_with_tags(show_studio / "01.flac", ARTIST="Test Band", ALBUM="LP")
    scan_studio = build_scan_payload(str(show_studio))
    assert classify_kind(scan_studio, "LP", None) == "studio"


# ── Parity: batch_scan, the bulk worker, and build_scan_payload's own
#    "resolved" key must all agree for the same folder (spec section 10) ────

def test_batch_scan_bulk_worker_and_build_scan_payload_resolved_agree(app, tmp_path):
    from app.extensions import db as _db
    from app.models.recording import Recording
    from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
    from app.utils import bulk_ingest_run

    show = _metheny_folder(tmp_path)
    app.config["LIBRARY_ROOT"] = str(tmp_path)

    # 1. build_scan_payload's own "resolved" key.
    scan = build_scan_payload(str(show))
    payload_resolved = scan["resolved"]
    assert payload_resolved is not None
    assert payload_resolved["date"]["value"] == {"year": 1979, "month": 6, "day": 14}
    assert payload_resolved["venue"]["value"] == "Stars"

    # 2. batch_scan's HTTP response.
    app.config["LOGIN_DISABLED"] = True
    client = app.test_client()
    resp = client.post("/api/ingest/batch-scan", json={"source_dir": str(tmp_path)})
    assert resp.status_code == 200
    items = resp.get_json()["items"]
    row = next(r for r in items if r["path"] == str(show))
    assert row["extracted"]["year"] == 1979
    assert row["extracted"]["month"] == 6
    assert row["extracted"]["day"] == 14
    assert row["extracted"]["venue"] == "Stars"
    assert row["tier"] == "green"

    # 3. The bulk worker, over the same folder.
    def _never_stop():
        return False

    run = BulkIngestRun(root=str(tmp_path), status="running")
    _db.session.add(run)
    _db.session.commit()
    bulk_ingest_run.discover(run)
    bulk_ingest_run.process(run, _never_stop)

    item = (_db.session.query(BulkIngestItem)
           .filter_by(run_id=run.id, rel_path=show.name).first())
    assert item.status == "ingested"
    rec = _db.session.get(Recording, item.recording_id)
    assert (rec.performance.start_year, rec.performance.start_month,
           rec.performance.start_day) == (1979, 6, 14)
    assert rec.performance.venue.name == "Stars"
