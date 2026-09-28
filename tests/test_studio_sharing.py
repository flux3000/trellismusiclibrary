"""
tests/test_studio_sharing.py -- Studio Records spec v1, section 1 (sharing
policy) -- REVERSED 2026-09-27 (Ryan): the share_override switch is gone and
a studio recording now shares exactly like a live one. No per-recording
opt-in, no PUT field, no migration column.

Covers:
  * both authorization paths (`peer_visible_recording_ids`, the visible set,
    and `peer_can_access_recording_id`, the milestone-1 per-recording check)
    treating a studio recording exactly like a live one -- no special-casing
    left in either implementation
  * the equivalence test between the two paths, still covering studio cases
  * is_published=False still hides a recording of either kind (through the
    Full Library system collection -- the one path that filters on it)
  * every share-door list/detail route that derives from the visible set
    showing a studio recording just as it would a live one
  * PUT /api/recordings/<id> with a share_override field is simply ignored
    (there is no such column any more)
  * scripts/migrate_studio_records.py: dry run, apply, no-op on a temp DB --
    the share_override column step is gone; the script still adds its other
    columns
"""

import subprocess
import sys
import sqlite3
from pathlib import Path

import pytest

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.collection import Collection, CollectionRecording, SYSTEM_FULL_LIBRARY
from app.models.peer import Peer, CollectionGrant, PeerToken
from app.models.user import User

from app.utils.peer_auth import generate_token, hash_secret
from app.utils.peer_access import (
    peer_can_access_recording_id,
    peer_visible_recording_ids,
)
from app.utils.serialize import recording_summary

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "migrate_studio_records.py"


# -- helpers -----------------------------------------------------------------

def _recording(label, kind="live", published=True):
    artist = Artist(name=f"{label} Band")
    _db.session.add(artist)
    _db.session.flush()

    perf = Performance(artist_id=artist.id, start_year=1999, start_month=1, start_day=1)
    _db.session.add(perf)
    _db.session.flush()

    rec = Recording(performance_id=perf.id, source="SBD", is_complete=True,
                    is_official=False, folder_path=f"{label}/1999",
                    is_published=published, kind=kind)
    _db.session.add(rec)
    _db.session.commit()
    return rec


def _peer_granted_junction(rec, name="Matt", also=()):
    """A peer holding a live grant to an ordinary (junction-backed) collection
    that contains `rec` (plus any recordings in `also`)."""
    col = Collection(name=f"Box for {rec.id}")
    _db.session.add(col)
    _db.session.flush()
    _db.session.add(CollectionRecording(collection_id=col.id, recording_id=rec.id, order=0))
    for other in also:
        _db.session.add(CollectionRecording(collection_id=col.id, recording_id=other.id, order=1))
    peer = Peer(name=name)
    _db.session.add(peer)
    _db.session.flush()
    _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=col.id))
    raw = generate_token()
    _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()
    return col, peer, raw


def _peer_granted_full_library(name="Matt"):
    """A peer holding a live grant to the Full Library system collection --
    the one path that actually filters on is_published (see
    app/utils/peer_access.py's _system_collection_ids_containing)."""
    col = Collection(name="Full Library", system_key=SYSTEM_FULL_LIBRARY)
    _db.session.add(col)
    _db.session.flush()
    peer = Peer(name=name)
    _db.session.add(peer)
    _db.session.flush()
    _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=col.id))
    raw = generate_token()
    _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()
    return col, peer, raw


def _second_recording_same_artist(rec, label, kind="live"):
    """A second recording under the SAME artist as `rec`, on its own
    Performance -- so the artist itself stays visible (peer_can_access_artist
    needs at least one visible recording) while the studio recording under
    test can be asserted absent or present in its recording list."""
    perf = Performance(artist_id=rec.performance.artist_id,
                       start_year=2000, start_month=1, start_day=1)
    _db.session.add(perf)
    _db.session.flush()
    other = Recording(performance_id=perf.id, source="SBD", is_complete=True,
                      is_official=False, folder_path=f"{label}/2000",
                      is_published=True, kind=kind)
    _db.session.add(other)
    _db.session.commit()
    return other


def _auth(raw):
    return {"Authorization": f"Bearer {raw}"}


def _login_as(client, username="admin"):
    """Session login that PROVES it authenticated -- see the long note in
    test_peer_sharing._login_as."""
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None, f"no such user to log in as: {username!r}"
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    probe = client.get("/api/collections/")
    assert probe.status_code == 200, (
        f"login did not take: /api/collections/ returned {probe.status_code}")


# -- the two authorization paths, individually --------------------------------

def test_studio_recording_visible_on_the_visible_set_path(app):
    rec = _recording("Alpha", kind="studio")
    col, peer, raw = _peer_granted_junction(rec)
    assert rec.id in peer_visible_recording_ids(peer)


def test_live_recording_visible_on_the_visible_set_path(app):
    rec = _recording("Bravo", kind="live")
    col, peer, raw = _peer_granted_junction(rec)
    assert rec.id in peer_visible_recording_ids(peer)


def test_studio_recording_visible_on_the_milestone1_path(app):
    rec = _recording("Charlie", kind="studio")
    col, peer, raw = _peer_granted_junction(rec)
    assert peer_can_access_recording_id(peer, rec.id) is True


def test_live_recording_visible_on_the_milestone1_path(app):
    rec = _recording("Delta", kind="live")
    col, peer, raw = _peer_granted_junction(rec)
    assert peer_can_access_recording_id(peer, rec.id) is True


# -- is_published still hides either kind (Full Library, the one path that
#    filters on it) ------------------------------------------------------------

def test_unpublished_studio_recording_hidden_via_full_library(app):
    rec = _recording("Echo", kind="studio", published=False)
    col, peer, raw = _peer_granted_full_library()
    assert rec.id not in peer_visible_recording_ids(peer)


def test_unpublished_live_recording_hidden_via_full_library(app):
    rec = _recording("Foxtrot", kind="live", published=False)
    col, peer, raw = _peer_granted_full_library()
    assert rec.id not in peer_visible_recording_ids(peer)


def test_published_studio_recording_visible_via_full_library(app):
    rec = _recording("Golf", kind="studio", published=True)
    col, peer, raw = _peer_granted_full_library()
    assert rec.id in peer_visible_recording_ids(peer)


# -- the equivalence test, still covering studio cases -------------------------

def test_both_paths_agree_including_studio_cases(app):
    """`peer_can_access_recording_id` (milestone 1) and
    `peer_visible_recording_ids` (milestone 2) are separate implementations.
    Disagreement here means a peer either sees something unplayable or plays
    something unlisted -- checked here for both a studio and a live
    recording, granted and unpublished-and-hidden alike.
    """
    studio       = _recording("Hotel",  kind="studio")
    live         = _recording("India",  kind="live")
    unpublished  = _recording("Juliet", kind="studio", published=False)
    col = Collection(name="Everything", system_key=SYSTEM_FULL_LIBRARY)
    _db.session.add(col)
    _db.session.flush()
    peer = Peer(name="Matt")
    _db.session.add(peer)
    _db.session.flush()
    _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=col.id))
    raw = generate_token()
    _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()

    visible = peer_visible_recording_ids(peer)
    assert studio.id in visible
    assert live.id in visible
    assert unpublished.id not in visible

    for rec in (studio, live, unpublished):
        assert peer_can_access_recording_id(peer, rec.id) == (rec.id in visible), (
            f"paths disagree on recording {rec.id} ({rec.kind})")


# -- the share door: every list/detail route derives from the visible set -----

def test_studio_recording_present_in_artist_recordings_over_share_door(app):
    rec = _recording("Kilo", kind="studio")
    sibling = _second_recording_same_artist(rec, "Kilo", kind="live")
    col, peer, raw = _peer_granted_junction(rec, also=(sibling,))
    client = app.test_client()

    res = client.get(f"/api/share/artists/{rec.performance.artist_id}/recordings",
                     headers=_auth(raw))
    assert res.status_code == 200
    ids = [r["id"] for perf in res.get_json() for r in perf["recordings"]]
    assert sibling.id in ids
    assert rec.id in ids


def test_studio_recording_detail_200s_over_share_door(app):
    rec = _recording("Lima", kind="studio")
    col, peer, raw = _peer_granted_junction(rec)
    client = app.test_client()
    assert client.get(f"/api/share/recordings/{rec.id}",
                      headers=_auth(raw)).status_code == 200


# -- PUT /api/recordings/<id>: a share_override field is simply ignored -------

def test_put_share_override_is_ignored_no_such_column(app):
    rec = _recording("Mike", kind="studio")
    client = app.test_client()
    _login_as(client)

    res = client.put(f"/api/recordings/{rec.id}", json={"share_override": True})
    assert res.status_code == 200
    detail = client.get(f"/api/recordings/{rec.id}").get_json()
    assert "share_override" not in detail
    assert "share_override" not in recording_summary(rec)


# -- scripts/migrate_studio_records.py -----------------------------------------

def _make_migration_db(path):
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE recording (
            id INTEGER PRIMARY KEY,
            folder_path TEXT
        );
        """
    )
    con.execute("INSERT INTO recording (id, folder_path) VALUES (1, 'Artist/Show')")
    con.execute("INSERT INTO recording (id, folder_path) VALUES (2, 'Artist/Show2')")
    con.commit()
    con.close()


def _run_migration(db_path, apply=False):
    args = [sys.executable, str(SCRIPT), "--db", str(db_path)]
    if apply:
        args.append("--apply")
    return subprocess.run(args, capture_output=True, text=True, cwd=str(REPO_ROOT))


def test_migration_dry_run_changes_nothing(tmp_path):
    db_path = tmp_path / "studio.db"
    _make_migration_db(db_path)
    before = db_path.read_bytes()

    result = _run_migration(db_path, apply=False)
    after = db_path.read_bytes()

    assert result.returncode == 0, result.stdout + result.stderr
    assert before == after

    con = sqlite3.connect(str(db_path))
    cols = {r[1] for r in con.execute("PRAGMA table_info(recording)")}
    assert "mb_release_id" not in cols
    con.close()


def test_migration_apply_adds_columns_but_not_share_override(tmp_path):
    db_path = tmp_path / "studio.db"
    _make_migration_db(db_path)

    result = _run_migration(db_path, apply=True)
    assert result.returncode == 0, result.stdout + result.stderr

    con = sqlite3.connect(str(db_path))
    cols = {r[1] for r in con.execute("PRAGMA table_info(recording)")}
    assert "share_override" not in cols
    assert "mb_release_id" in cols
    con.close()


def test_migration_apply_is_idempotent(tmp_path):
    db_path = tmp_path / "studio.db"
    _make_migration_db(db_path)

    first = _run_migration(db_path, apply=True)
    assert first.returncode == 0, first.stdout + first.stderr
    after_first = db_path.read_bytes()

    second = _run_migration(db_path, apply=True)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already present" in second.stdout
    after_second = db_path.read_bytes()

    assert after_first == after_second


# -- B1 backfill: splitting a Performance shared by >1 studio recording ------

def _make_shared_performance_db(path):
    """A minimal but REAL-shaped schema (recording.kind + performance),
    unlike _make_migration_db's bare two-column stand-in above -- the B1
    split step reads both tables directly with raw sqlite3, so it needs
    them to exist for anything to test.

    Performance 10: two studio recordings (1, 2) sharing it -- the shape
    every studio ingest used to produce before app/api/ingest.py's fix.
    Performance 20: one studio recording (3) alone on it already -- must be
    left completely untouched.
    Performance 30: one live recording (4) -- kind='studio' filter must
    never touch it even though it's the sole recording on its Performance.
    """
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE performance (
            id INTEGER PRIMARY KEY,
            artist_id INTEGER NOT NULL,
            venue_id INTEGER,
            start_year INTEGER, start_month INTEGER, start_day INTEGER,
            end_year INTEGER, end_month INTEGER, end_day INTEGER,
            city TEXT, state TEXT, country TEXT,
            personnel_mode TEXT NOT NULL DEFAULT 'inherit'
        );
        CREATE TABLE recording (
            id INTEGER PRIMARY KEY,
            performance_id INTEGER NOT NULL,
            folder_path TEXT,
            kind TEXT NOT NULL DEFAULT 'live'
        );
    """)
    con.execute("INSERT INTO performance (id, artist_id, venue_id, start_year, city, state, country) "
                "VALUES (10, 1, 99, 1977, 'Boulder', 'CO', 'US')")
    con.execute("INSERT INTO performance (id, artist_id, start_year) VALUES (20, 1, 1978)")
    con.execute("INSERT INTO performance (id, artist_id, start_year) VALUES (30, 1, 1979)")
    con.execute("INSERT INTO recording (id, performance_id, folder_path, kind) VALUES (1, 10, 'A/1', 'studio')")
    con.execute("INSERT INTO recording (id, performance_id, folder_path, kind) VALUES (2, 10, 'A/2', 'studio')")
    con.execute("INSERT INTO recording (id, performance_id, folder_path, kind) VALUES (3, 20, 'A/3', 'studio')")
    con.execute("INSERT INTO recording (id, performance_id, folder_path, kind) VALUES (4, 30, 'A/4', 'live')")
    con.commit()
    con.close()


def test_b1_dry_run_lists_the_split_and_writes_nothing(tmp_path):
    db_path = tmp_path / "shared.db"
    _make_shared_performance_db(db_path)
    before = db_path.read_bytes()

    result = _run_migration(db_path, apply=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert db_path.read_bytes() == before
    assert "performance 10: keep recording 1, split off [2]" in result.stdout

    con = sqlite3.connect(str(db_path))
    assert con.execute("SELECT COUNT(*) FROM performance").fetchone()[0] == 3
    con.close()


def test_b1_apply_splits_recording_2_onto_its_own_performance(tmp_path):
    db_path = tmp_path / "shared.db"
    _make_shared_performance_db(db_path)

    result = _run_migration(db_path, apply=True)
    assert result.returncode == 0, result.stdout + result.stderr

    con = sqlite3.connect(str(db_path))
    # Recording 1 kept its original Performance.
    assert con.execute("SELECT performance_id FROM recording WHERE id=1").fetchone()[0] == 10
    # Recording 2 got a NEW one, copying artist/date, with venue_id null.
    new_perf_id = con.execute("SELECT performance_id FROM recording WHERE id=2").fetchone()[0]
    assert new_perf_id != 10
    row = con.execute("SELECT artist_id, start_year, venue_id, city, state, country "
                      "FROM performance WHERE id=?", (new_perf_id,)).fetchone()
    assert row == (1, 1977, None, "Boulder", "CO", "US")
    # Recording 3 (already alone) and recording 4 (live) are untouched.
    assert con.execute("SELECT performance_id FROM recording WHERE id=3").fetchone()[0] == 20
    assert con.execute("SELECT performance_id FROM recording WHERE id=4").fetchone()[0] == 30
    con.close()


def test_b1_apply_is_idempotent(tmp_path):
    db_path = tmp_path / "shared.db"
    _make_shared_performance_db(db_path)

    first = _run_migration(db_path, apply=True)
    assert first.returncode == 0, first.stdout + first.stderr
    after_first = db_path.read_bytes()

    second = _run_migration(db_path, apply=True)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "none found" in second.stdout
    assert db_path.read_bytes() == after_first


# -- S1: image_url batching over the share door --------------------------------

def _count_queries(fn, contains=None):
    """Run `fn()`, returning (result, number of SQL statements executed).

    `contains` narrows the count to statements whose SQL text contains that
    substring (case-insensitive) -- the route this test hits has its own
    unrelated per-performance N+1 (Performance.recordings, out of this
    review's scope), so counting every statement in the request would be
    confounded by that; counting only recording_image statements isolates
    the S1 fix specifically."""
    from sqlalchemy import event
    n = [0]

    def _tick(conn, cursor, statement, *a, **k):
        if contains is None or contains.lower() in statement.lower():
            n[0] += 1
    event.listen(_db.engine, "before_cursor_execute", _tick)
    try:
        result = fn()
    finally:
        event.remove(_db.engine, "before_cursor_execute", _tick)
    return result, n[0]


def test_share_all_recordings_one_image_query_for_whole_payload_s1(app):
    """S1: build_search_index()/all_recordings() must batch image urls with
    ONE grouped query for the whole payload, not one per image-less row --
    the review measured 501 recording_image queries for 500 image-less
    recordings on this exact route."""
    artist = Artist(name="Batch Band")
    _db.session.add(artist)
    _db.session.flush()
    recs = []
    for i in range(30):
        perf = Performance(artist_id=artist.id, start_year=2000, start_month=1, start_day=(i % 28) + 1)
        _db.session.add(perf)
        _db.session.flush()
        rec = Recording(performance_id=perf.id, source="SBD", is_complete=True,
                        is_official=False, folder_path=f"Batch/{i}",
                        is_published=True, kind="live")
        _db.session.add(rec)
        recs.append(rec)
    _db.session.commit()

    col = Collection(name="Everything")
    _db.session.add(col)
    _db.session.flush()
    for rec in recs:
        _db.session.add(CollectionRecording(collection_id=col.id, recording_id=rec.id, order=0))
    peer = Peer(name="Matt")
    _db.session.add(peer)
    _db.session.flush()
    _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=col.id))
    raw = generate_token()
    _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()

    client = app.test_client()
    # Count only statements hitting the recording_image TABLE (matching on
    # "from recording_image" rather than bare "recording_image", since
    # every plain `SELECT recording.*` row also aliases the unrelated
    # recording.images_checked_at column as "recording_images_checked_at",
    # which itself contains the substring "recording_image" and would
    # otherwise be miscounted). This route has its own unrelated
    # per-performance N+1 (Performance.recordings lazy-loads, out of S1's
    # scope) that would swamp an unfiltered count.
    resp, n_image_queries = _count_queries(
        lambda: client.get("/api/share/artists/all-recordings", headers=_auth(raw)),
        contains="from recording_image")
    assert resp.status_code == 200
    body = resp.get_json()
    total_recs = sum(len(p["recordings"]) for a in body for p in a["performances"])
    assert total_recs == 30
    # The image lookup must be a small constant number of queries, not one
    # per recording (30 recordings here, none with images) -- the review
    # measured 501 recording_image queries for 500 image-less recordings
    # on this exact route.
    assert n_image_queries <= 1, (
        f"{n_image_queries} recording_image queries for 30 image-less recordings")


def test_share_search_image_url_uses_share_prefix_s2(app):
    """S2: the peer search route must rewrite build_search_index()'s LOCAL
    image-url prefix into the share-door one -- an unrewritten local path
    404s on the peer's OWN library (or collides with one of their own image
    ids) instead of reaching this library's art through the proxy."""
    from app.models.recording_image import RecordingImage

    artist = Artist(name="ZQXSEARCHMARK Band")
    _db.session.add(artist)
    _db.session.flush()
    perf = Performance(artist_id=artist.id, start_year=2001, start_month=1, start_day=1)
    _db.session.add(perf)
    _db.session.flush()
    rec = Recording(performance_id=perf.id, source="SBD", is_complete=True,
                    is_official=False, folder_path="ZQX/1",
                    is_published=True, kind="live", title="ZQXSEARCHMARK Show")
    _db.session.add(rec)
    _db.session.flush()
    _db.session.add(RecordingImage(recording_id=rec.id, filename="x.jpg", ext=".jpg",
                                   is_primary=True, origin="upload"))
    col = Collection(name="Everything2")
    _db.session.add(col)
    _db.session.flush()
    _db.session.add(CollectionRecording(collection_id=col.id, recording_id=rec.id, order=0))
    peer = Peer(name="Matt2")
    _db.session.add(peer)
    _db.session.flush()
    _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=col.id))
    raw = generate_token()
    _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()

    client = app.test_client()
    res = client.get("/api/share/search?q=ZQXSEARCHMARK&type=recordings", headers=_auth(raw))
    assert res.status_code == 200
    body = res.get_json()
    urls = [item.get("image_url") for item in body.get("items", []) if item.get("image_url")]
    assert urls, "expected at least one image_url in the search results"
    assert all(u.startswith("/api/share/recordings/images/") for u in urls), urls


# -- N4/reversal check: studio artwork now reaches peers just like live -------

def test_peer_summary_carries_no_share_override_field(app):
    rec = _recording("Quebec", kind="studio")
    from app.api.share import _peer_summary
    row = _peer_summary(rec)
    assert "share_override" not in row
    assert "share_override" not in recording_summary(rec)
