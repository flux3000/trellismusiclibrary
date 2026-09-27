"""
tests/test_set_editing.py — Set editing on View Recording (spec section 6.4 /
7, chunk 11).

track.set_number is a plain, click-to-edit field: a PUT changes grouping and
the derived per-set track number the next GET returns, clears on an empty
value, and never touches disk, tags or track_number. set_track_number itself
is derived (spec section 5), so it is asserted only from a fresh GET/read,
never assumed to persist as its own column.
"""

import os

import pytest

from app.extensions import db as _db
from app.models.user import User
from app.models.track import Track


def _login_as(client, username="admin"):
    from app.extensions import login_manager
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        gen = getattr(login_manager, "_session_identifier_generator", None)
        if callable(gen):
            try:
                sess["_id"] = gen()
            except Exception:
                pass


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def three_tracks(app, seeded_ids):
    """
    The seeded recording (2 tracks) plus a third, so a two-into-one-set /
    one-into-another-set grouping is actually exercisable. track_number stays
    continuous 1, 2, 3; none has a set_number yet.
    """
    rec_id = seeded_ids["recording_id"]
    t1, t2 = _db.session.query(Track).filter_by(recording_id=rec_id).order_by(Track.track_number).all()
    t3 = Track(recording_id=rec_id, track_number=3, title="Waltz for Debby",
               duration=280, file_path="03.flac")
    _db.session.add(t3)
    _db.session.commit()
    return {"recording_id": rec_id, "t1": t1.id, "t2": t2.id, "t3": t3.id}


def _tracks_by_id(payload):
    return {t["id"]: t for t in payload["tracks"]}


def test_put_set_number_changes_grouping_and_derived_number(client, three_tracks):
    _login_as(client)
    rec_id, t1, t2, t3 = (three_tracks[k] for k in ("recording_id", "t1", "t2", "t3"))

    for tid, label in ((t1, "Set 1"), (t2, "Set 1"), (t3, "Set 2")):
        resp = client.put(f"/api/tracks/{tid}", json={"set_number": label})
        assert resp.status_code == 200, resp.get_data(as_text=True)

    payload = client.get(f"/api/recordings/{rec_id}").get_json()
    by_id = _tracks_by_id(payload)

    assert by_id[t1]["set_number"] == "Set 1"
    assert by_id[t2]["set_number"] == "Set 1"
    assert by_id[t3]["set_number"] == "Set 2"
    # Derived per-set position: 1-based within each label, by track_number.
    assert by_id[t1]["set_track_number"] == 1
    assert by_id[t2]["set_track_number"] == 2
    assert by_id[t3]["set_track_number"] == 1

    # Re-labelling regroups AND renumbers on the very next read, with no
    # separate "regroup" step -- the number is derived, never stored.
    resp = client.put(f"/api/tracks/{t2}", json={"set_number": "Set 2"})
    assert resp.status_code == 200
    payload = client.get(f"/api/recordings/{rec_id}").get_json()
    by_id = _tracks_by_id(payload)
    assert by_id[t1]["set_track_number"] == 1        # alone in Set 1 now
    # Set 2 now holds t3 (track_number 3) and t2 (track_number 2), ordered
    # by track_number: t2 first, then t3.
    assert by_id[t2]["set_track_number"] == 1
    assert by_id[t3]["set_track_number"] == 2


def test_empty_value_clears_set_number(client, three_tracks):
    _login_as(client)
    t1 = three_tracks["t1"]
    rec_id = three_tracks["recording_id"]

    assert client.put(f"/api/tracks/{t1}", json={"set_number": "Set 1"}).status_code == 200
    resp = client.put(f"/api/tracks/{t1}", json={"set_number": ""})
    assert resp.status_code == 200

    payload = client.get(f"/api/recordings/{rec_id}").get_json()
    by_id = _tracks_by_id(payload)
    assert by_id[t1]["set_number"] is None
    assert by_id[t1]["set_track_number"] is None


def test_flat_list_has_no_set_track_number_when_no_track_has_a_set(client, three_tracks):
    _login_as(client)
    rec_id = three_tracks["recording_id"]
    payload = client.get(f"/api/recordings/{rec_id}").get_json()
    assert all(t["set_track_number"] is None for t in payload["tracks"])
    assert all(t["set_number"] is None for t in payload["tracks"])


def test_set_edit_touches_no_disk_tag_or_track_number(client, three_tracks, tmp_path, app):
    """Setting a track's set does not rename its file, does not write a
    tags_written event, and never changes track_number (spec section 6.4:
    "Set editing does not touch disk, tags or track_number")."""
    _login_as(client)
    rec_id, t1 = three_tracks["recording_id"], three_tracks["t1"]

    library_root = tmp_path
    app.config["LIBRARY_ROOT"] = str(library_root)
    rec_dir = library_root / "Bill Evans" / "1980"
    rec_dir.mkdir(parents=True)
    (rec_dir / "01.flac").write_bytes(b"original-audio")

    before = _db.session.get(Track, t1)
    before_file_path = before.file_path
    before_track_number = before.track_number

    resp = client.put(f"/api/tracks/{t1}", json={"set_number": "Set 1"})
    assert resp.status_code == 200

    after = _db.session.get(Track, t1)
    assert after.file_path == before_file_path
    assert after.track_number == before_track_number
    assert (rec_dir / "01.flac").read_bytes() == b"original-audio"

    events = client.get(f"/api/recordings/{rec_id}").get_json()["events"]
    assert not any(e["event_type"] == "tags_written" for e in events)
