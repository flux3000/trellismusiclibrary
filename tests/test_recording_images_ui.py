"""
tests/test_recording_images_ui.py -- Studio Records spec v1 chunk 5: the UI
surface on top of the artwork ingestion chunk 4 already covers --
register_image_routes() for recordings, image_url on recording_summary/
recording_row/search, and the share-door serve route's visibility check.
"""

import io

import pytest


def _upload_images(res):
    """handle_upload() returns {"images": [...], "errors": [...]}, not a bare list."""
    body = res.get_json()
    return body["images"]

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.recording_image import RecordingImage
from app.models.collection import Collection, CollectionRecording
from app.models.peer import Peer, CollectionGrant, PeerToken
from app.models.user import User
from app.utils.peer_auth import generate_token, hash_secret
from app.utils.serialize import recording_summary, recording_row


_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"jpegdata" * 8
_PNG_BYTES  = b"\x89PNG\r\n\x1a\n" + b"pngdata" * 8


def _recording(label="Alpha", kind="studio"):
    artist = _db.session.query(Artist).filter_by(name="Bill Evans").first()
    if artist is None:
        artist = Artist(name="Bill Evans")
        _db.session.add(artist)
        _db.session.flush()
    perf = Performance(artist_id=artist.id, start_year=1999)
    _db.session.add(perf)
    _db.session.flush()
    rec = Recording(performance_id=perf.id, folder_path=f"{label}/1999",
                    is_complete=True, is_official=False,
                    is_published=True, kind=kind)
    _db.session.add(rec)
    _db.session.commit()
    return rec


def _login_as(client, username="admin"):
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None, f"no such user to log in as: {username!r}"
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    probe = client.get("/api/collections/")
    assert probe.status_code == 200, (
        f"login did not take: /api/collections/ returned {probe.status_code}")


# ── register_image_routes(): upload / list / primary / delete ──────────────

def test_upload_stores_under_recordings_dir_and_lists(app, seeded_ids, tmp_path):
    app.config["DATA_DIR"] = str(tmp_path / "data_test_upload_stores_under_recordings_dir_and_lists")
    rec = _recording("Bravo")
    client = app.test_client()
    _login_as(client)

    res = client.post(f"/api/recordings/{rec.id}/images",
                      data={"image": (io.BytesIO(_JPEG_BYTES), "cover.jpg")},
                      content_type="multipart/form-data")
    assert res.status_code == 200, res.get_data(as_text=True)
    body = _upload_images(res)
    assert body[0]["is_primary"] is True   # first upload becomes primary

    from app.utils.entity_images import image_dir
    with app.app_context():
        d = image_dir("recordings", str(rec.id))
    stored = list(d.iterdir())
    assert len(stored) == 1
    assert stored[0].read_bytes() == _JPEG_BYTES

    listed = client.get(f"/api/recordings/{rec.id}/images").get_json()
    assert len(listed) == 1
    assert listed[0]["url"] == f"/api/recordings/images/{listed[0]['id']}"


def test_set_primary_and_delete(app, seeded_ids, tmp_path):
    app.config["DATA_DIR"] = str(tmp_path / "data_test_set_primary_and_delete")
    rec = _recording("Charlie")
    client = app.test_client()
    _login_as(client)

    client.post(f"/api/recordings/{rec.id}/images",
               data={"image": (io.BytesIO(_JPEG_BYTES), "a.jpg")},
               content_type="multipart/form-data")
    res2 = client.post(f"/api/recordings/{rec.id}/images",
                       data={"image": (io.BytesIO(_PNG_BYTES), "b.png")},
                       content_type="multipart/form-data")
    imgs = _upload_images(res2)
    second_id = imgs[-1]["id"]
    assert imgs[-1]["is_primary"] is False   # only the FIRST upload is primary

    res = client.post(f"/api/recordings/images/{second_id}/primary")
    assert res.status_code == 200
    assert res.get_json()["is_primary"] is True

    listed = client.get(f"/api/recordings/{rec.id}/images").get_json()
    primaries = [i for i in listed if i["is_primary"]]
    assert len(primaries) == 1
    assert primaries[0]["id"] == second_id

    first_id = [i["id"] for i in listed if i["id"] != second_id][0]
    del_res = client.delete(f"/api/recordings/images/{first_id}")
    assert del_res.status_code == 200
    assert del_res.get_json()["ok"] is True
    listed_after = client.get(f"/api/recordings/{rec.id}/images").get_json()
    assert len(listed_after) == 1
    assert listed_after[0]["id"] == second_id


def test_serve_route_returns_stored_bytes(app, seeded_ids, tmp_path):
    app.config["DATA_DIR"] = str(tmp_path / "data_test_serve_route_returns_stored_bytes")
    rec = _recording("Delta")
    client = app.test_client()
    _login_as(client)
    upload = _upload_images(client.post(f"/api/recordings/{rec.id}/images",
                        data={"image": (io.BytesIO(_JPEG_BYTES), "cover.jpg")},
                        content_type="multipart/form-data"))
    image_id = upload[0]["id"]
    res = client.get(f"/api/recordings/images/{image_id}")
    assert res.status_code == 200
    assert res.data == _JPEG_BYTES


# ── image_url on recording_summary / recording_row ─────────────────────────

def test_image_url_present_in_summary_and_row(app, seeded_ids, tmp_path):
    app.config["DATA_DIR"] = str(tmp_path / "data_test_image_url_present_in_summary_and_row")
    rec = _recording("Echo")
    client = app.test_client()
    _login_as(client)
    upload = _upload_images(client.post(f"/api/recordings/{rec.id}/images",
                        data={"image": (io.BytesIO(_JPEG_BYTES), "cover.jpg")},
                        content_type="multipart/form-data"))
    image_id = upload[0]["id"]

    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert recording_summary(rec)["image_url"] == f"/api/recordings/images/{image_id}"
    assert recording_row(rec)["image_url"] == f"/api/recordings/images/{image_id}"


def test_image_url_null_when_no_image(app, seeded_ids):
    rec = _recording("Foxtrot")
    assert recording_summary(rec)["image_url"] is None
    assert recording_row(rec)["image_url"] is None


def test_get_recording_detail_carries_images_and_image_url(app, seeded_ids, tmp_path):
    app.config["DATA_DIR"] = str(tmp_path / "data_test_get_recording_detail_carries_images_and_image_url")
    rec = _recording("Golf")
    client = app.test_client()
    _login_as(client)
    upload = _upload_images(client.post(f"/api/recordings/{rec.id}/images",
                        data={"image": (io.BytesIO(_JPEG_BYTES), "cover.jpg")},
                        content_type="multipart/form-data"))
    image_id = upload[0]["id"]

    detail = client.get(f"/api/recordings/{rec.id}").get_json()
    assert detail["image_url"] == f"/api/recordings/images/{image_id}"
    assert len(detail["images"]) == 1
    assert detail["images"][0]["id"] == image_id


# ── Peer door: visibility-gated serve ─────────────────────────────────────

def _peer_granted_junction(rec, name="Matt"):
    col = Collection(name=f"Box for {rec.id}")
    _db.session.add(col)
    _db.session.flush()
    _db.session.add(CollectionRecording(collection_id=col.id, recording_id=rec.id, order=0))
    peer = Peer(name=name)
    _db.session.add(peer)
    _db.session.flush()
    _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=col.id))
    raw = generate_token()
    _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()
    return peer, raw


def test_share_door_image_200_for_visible_studio_recording(app, seeded_ids, tmp_path):
    # REVERSED 2026-09-27 (Ryan): a studio recording's artwork reaches peers
    # the same as a live recording's -- there is no per-recording sharing
    # switch any more, so a studio recording visible to a peer at all (via
    # an ordinary collection grant) shows its artwork too.
    app.config["DATA_DIR"] = str(tmp_path / "data_test_share_door_image_200_for_visible_studio_recording")
    rec = _recording("Hotel", kind="studio")
    client = app.test_client()
    _login_as(client)
    upload = _upload_images(client.post(f"/api/recordings/{rec.id}/images",
                        data={"image": (io.BytesIO(_JPEG_BYTES), "cover.jpg")},
                        content_type="multipart/form-data"))
    image_id = upload[0]["id"]

    _peer, raw = _peer_granted_junction(rec)
    res = client.get(f"/api/share/recordings/images/{image_id}",
                     headers={"Authorization": f"Bearer {raw}"})
    assert res.status_code == 200
    assert res.data == _JPEG_BYTES
