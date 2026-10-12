"""tests/test_share_policy.py -- which recordings may ever reach a peer.

Matrix: kind, is_official, MusicBrainz link + stored status. Checked on both
authorization paths, the SQL and Python forms of the rule, every share-door
route that returns or counts recordings, the stream route, and the
"disappears, never deleted" behaviour for an already-shared record.
"""

import pytest

from app.extensions import db as _db
from app.models.track import Track
from app.utils.peer_access import (
    peer_can_access_recording_id, peer_visible_recording_ids,
    peer_visible_artist_ids,
)
from app.utils.share_policy import is_shareable, shareable_recording_ids

from tests.test_studio_sharing import (
    _recording, _peer_granted_junction, _peer_granted_full_library,
    _second_recording_same_artist, _auth,
)


def _set(rec, **kw):
    for k, v in kw.items():
        setattr(rec, k, v)
    _db.session.commit()
    return rec


CASES = [
    # (id, kwargs, shareable)
    ("live_plain",            dict(), True),
    ("live_official_flag",    dict(is_official=True), False),
    ("studio_plain",          dict(kind="studio"), False),
    ("live_linked_official",  dict(mb_release_id="x", mb_release_db_status="Official"), False),
    ("live_linked_bootleg",   dict(mb_release_id="x", mb_release_db_status="Bootleg"), True),
    ("live_linked_promotion", dict(mb_release_id="x", mb_release_db_status="Promotion"), False),
    ("live_linked_unknown",   dict(mb_release_id="x", mb_release_db_status=None), False),
    ("live_unlinked_stale_status", dict(mb_release_db_status="Official"), True),
]


@pytest.mark.parametrize("name,kw,expected", CASES, ids=[c[0] for c in CASES])
def test_policy_matrix_agrees_everywhere(app, name, kw, expected):
    rec = _set(_recording(name), **kw)
    assert is_shareable(rec) is expected
    assert (rec.id in shareable_recording_ids()) is expected   # SQL form
    _col, peer, _raw = _peer_granted_junction(rec)
    assert (rec.id in peer_visible_recording_ids(peer)) is expected
    assert peer_can_access_recording_id(peer, rec.id) is expected
    # Full Library path
    _c2, peer2, _r2 = _peer_granted_full_library(name="Other")
    assert (rec.id in peer_visible_recording_ids(peer2)) is expected
    assert peer_can_access_recording_id(peer2, rec.id) is expected


def test_official_record_blocked_on_every_share_route(app):
    official = _set(_recording("Off"), is_official=True)
    ok = _second_recording_same_artist(official, "Off", kind="live")
    t_off = Track(recording_id=official.id, track_number=1, title="a",
                  file_path="a.flac")
    _db.session.add(t_off)
    _db.session.commit()
    col, peer, raw = _peer_granted_junction(official, also=(ok,))
    client = app.test_client()
    h = _auth(raw)

    assert client.get(f"/api/share/recordings/{official.id}", headers=h).status_code == 403
    assert client.get(f"/api/share/stream/{t_off.id}", headers=h).status_code == 403
    assert client.get(f"/api/share/recordings/{ok.id}", headers=h).status_code == 200

    for url in (f"/api/share/collections/{col.id}",
                "/api/share/recordings/recent",
                f"/api/share/artists/{official.performance.artist_id}/recordings",
                "/api/share/artists/all-recordings"):
        res = client.get(url, headers=h)
        assert res.status_code == 200, url
        flat = res.get_json()
        assert f"'id': {official.id}," not in str(flat), url
    by_ids = client.get(f"/api/share/recordings/by-ids?ids={official.id},{ok.id}",
                        headers=h).get_json()
    assert [r["id"] for r in by_ids] == [ok.id]

    cols = client.get("/api/share/collections", headers=h).get_json()
    assert cols[0]["recording_count"] == 1


def test_artist_with_only_withheld_records_disappears(app):
    rec = _recording("Solo", kind="studio")
    col, peer, raw = _peer_granted_junction(rec)
    assert rec.performance.artist_id not in peer_visible_artist_ids(peer)
    client = app.test_client()
    assert client.get(f"/api/share/artists/{rec.performance.artist_id}",
                      headers=_auth(raw)).status_code == 403


def test_already_shared_record_vanishes_then_returns_without_deletion(app):
    rec = _recording("Flip")
    col, peer, raw = _peer_granted_junction(rec)
    client = app.test_client()
    assert client.get(f"/api/share/recordings/{rec.id}", headers=_auth(raw)).status_code == 200

    _set(rec, is_official=True)
    assert client.get(f"/api/share/recordings/{rec.id}", headers=_auth(raw)).status_code == 403
    # Nothing deleted: row, collection link and grant are all intact.
    assert _db.session.get(type(rec), rec.id) is not None
    assert len(col.recording_links) == 1
    assert len(peer.grants) == 1

    _set(rec, is_official=False)
    assert client.get(f"/api/share/recordings/{rec.id}", headers=_auth(raw)).status_code == 200


def test_apply_to_recording_stores_and_unlink_clears_musicbrainz_status(app):
    from app.utils import musicbrainz as mb
    rec = _recording("Linked", kind="studio")
    mb.apply_to_recording(rec, {"mbid": "m1", "status": "Official", "tracks": []},
                          status="linked")
    assert rec.mb_release_db_status == "Official"
    mb.unlink_release(rec)
    assert rec.mb_release_db_status is None
    assert rec.mb_release_id is None


def test_migration_script_dry_run_then_apply_is_idempotent(tmp_path):
    import sqlite3, subprocess, sys
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "scripts" / "migrate_mb_release_db_status.py"
    path = tmp_path / "t.db"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO recording (id) VALUES (1)")
    con.commit()
    con.close()

    def run(*a):
        return subprocess.run([sys.executable, str(script), "--db", str(path), *a],
                              capture_output=True, text=True)

    assert run().returncode == 0
    cols = {r[1] for r in sqlite3.connect(str(path)).execute("PRAGMA table_info(recording)")}
    assert "mb_release_db_status" not in cols
    r = run("--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    cols = {r[1] for r in sqlite3.connect(str(path)).execute("PRAGMA table_info(recording)")}
    assert "mb_release_db_status" in cols
    assert "already present" in run("--apply").stdout


def test_sharing_pane_carries_approved_copy():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "app.js").read_text()
    assert "TODO_COPY" not in src
    assert "Sharing Guidelines" in src
    assert "You are responsible for what you share." in src
