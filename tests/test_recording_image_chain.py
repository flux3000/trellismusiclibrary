"""
tests/test_recording_image_chain.py -- the recording image is a chain, resolved
server-side in one place (Ryan, 2026-10-04): the recording's own primary image,
else its artist's primary image, else None (the client draws the artist's
initials). Covers the serializers, the batch helper, the search index, the
share door's prefixes, and POST /api/recordings/<id>/images/from-artist.
"""

import io
from pathlib import Path

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.artist_image import ArtistImage
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.recording_image import RecordingImage
from app.models.user import User
from app.utils.serialize import (batch_recording_image_urls, recording_row,
                                 recording_summary)

_JPEG = b"\xff\xd8\xff\xe0" + b"jpegdata" * 8
_PNG = b"\x89PNG\r\n\x1a\n" + b"pngdata" * 8


def _recording(name="Chain Artist", label="chain"):
    artist = _db.session.query(Artist).filter_by(name=name).first()
    if artist is None:
        artist = Artist(name=name)
        _db.session.add(artist)
        _db.session.flush()
    perf = Performance(artist_id=artist.id, start_year=1999)
    _db.session.add(perf)
    _db.session.flush()
    rec = Recording(performance_id=perf.id, folder_path=f"{label}/{perf.id}",
                    is_complete=True, is_official=False, is_published=True,
                    kind="live")
    _db.session.add(rec)
    _db.session.commit()
    return rec


def _login(client):
    user = _db.session.query(User).filter_by(username="admin").first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _upload(client, url, data=_JPEG, name="a.jpg"):
    res = client.post(url, data={"image": (io.BytesIO(data), name)},
                      content_type="multipart/form-data")
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()["images"][0]


def _setup(app, tmp_path, label):
    app.config["DATA_DIR"] = str(tmp_path / label)
    rec = _recording(label=label)
    client = app.test_client()
    _login(client)
    return rec, client


def test_no_image_anywhere_is_none(app, seeded_ids, tmp_path):
    rec, _ = _setup(app, tmp_path, "none")
    assert recording_summary(rec)["image_url"] is None
    assert recording_row(rec)["image_url"] is None
    assert batch_recording_image_urls([rec.id]) == {}


def test_artist_image_is_inherited(app, seeded_ids, tmp_path):
    rec, client = _setup(app, tmp_path, "inherit")
    img = _upload(client, f"/api/artists/{rec.performance.artist_id}/images")
    want = f"/api/artists/images/{img['id']}"
    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert recording_summary(rec)["image_url"] == want
    assert recording_row(rec)["image_url"] == want
    assert batch_recording_image_urls([rec.id]) == {rec.id: want}
    assert client.get(f"/api/recordings/{rec.id}").get_json()["image_url"] == want


def test_own_image_beats_artist_image(app, seeded_ids, tmp_path):
    rec, client = _setup(app, tmp_path, "own")
    _upload(client, f"/api/artists/{rec.performance.artist_id}/images")
    own = _upload(client, f"/api/recordings/{rec.id}/images", _PNG, "b.png")
    want = f"/api/recordings/images/{own['id']}"
    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert recording_row(rec)["image_url"] == want
    assert batch_recording_image_urls([rec.id]) == {rec.id: want}


def test_batch_mixes_own_inherited_and_none(app, seeded_ids, tmp_path):
    a, client = _setup(app, tmp_path, "mix")
    b = _recording(name="Other Artist", label="mix-b")
    c = _recording(name="Chain Artist", label="mix-c")   # same artist as a
    img = _upload(client, f"/api/artists/{a.performance.artist_id}/images")
    own = _upload(client, f"/api/recordings/{c.id}/images", _PNG, "c.png")
    got = batch_recording_image_urls([a.id, b.id, c.id])
    assert got == {a.id: f"/api/artists/images/{img['id']}",
                   c.id: f"/api/recordings/images/{own['id']}"}


def test_share_door_prefixes_both_links(app, seeded_ids, tmp_path):
    from app.api.share import _peer_image_urls, _peer_row
    a, client = _setup(app, tmp_path, "share")
    b = _recording(label="share-b")
    img = _upload(client, f"/api/artists/{a.performance.artist_id}/images")
    own = _upload(client, f"/api/recordings/{b.id}/images", _PNG, "s.png")
    _db.session.expire_all()
    a, b = _db.session.get(Recording, a.id), _db.session.get(Recording, b.id)
    urls = _peer_image_urls([a, b])
    assert urls[b.id] == f"/api/share/recordings/images/{own['id']}"
    # a has no image of its own: it inherits the artist's, with the SHARE prefix.
    assert _peer_row(a)["image_url"] == f"/api/share/artists/images/{img['id']}"
    assert urls[a.id] == f"/api/share/artists/images/{img['id']}"


def test_search_index_falls_back_to_artist(app, seeded_ids, tmp_path):
    from app.api.search import build_search_index
    rec, client = _setup(app, tmp_path, "search")
    img = _upload(client, f"/api/artists/{rec.performance.artist_id}/images")
    idx = build_search_index()
    row = next(r for r in idx["recordings"] if r["id"] == rec.id)
    assert row["image_url"] == f"/api/artists/images/{img['id']}"


# ── POST /api/recordings/<id>/images/from-artist ───────────────────────────

def test_use_artist_image_copies_file_and_sets_primary(app, seeded_ids, tmp_path):
    rec, client = _setup(app, tmp_path, "copy")
    aid = rec.performance.artist_id
    art = _upload(client, f"/api/artists/{aid}/images")
    row = _db.session.get(ArtistImage, art["id"])
    row.credit, row.caption = "Photo: Someone, CC BY", "Live 1999"
    _db.session.commit()

    res = client.post(f"/api/recordings/{rec.id}/images/from-artist")
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["is_primary"] is True and body["origin"] == "artist"
    assert body["credit"] == "Photo: Someone, CC BY"

    _db.session.expire_all()
    new = _db.session.get(RecordingImage, body["id"])
    assert new.recording_id == rec.id and new.caption == "Live 1999"
    # A real copy under the recording's own folder, not a reference.
    dest = Path(app.config["DATA_DIR"]) / "images" / "recordings" / str(rec.id) / new.filename
    assert dest.read_bytes() == _JPEG
    assert client.get(f"/api/recordings/{rec.id}").get_json()["image_url"] \
        == f"/api/recordings/images/{new.id}"


def test_use_artist_image_is_idempotent_and_repromotes(app, seeded_ids, tmp_path):
    rec, client = _setup(app, tmp_path, "idem")
    _upload(client, f"/api/artists/{rec.performance.artist_id}/images")
    first = client.post(f"/api/recordings/{rec.id}/images/from-artist").get_json()
    other = _upload(client, f"/api/recordings/{rec.id}/images", _PNG, "x.png")
    client.post(f"/api/recordings/images/{other['id']}/primary")
    again = client.post(f"/api/recordings/{rec.id}/images/from-artist").get_json()
    assert again["id"] == first["id"] and again["is_primary"] is True
    _db.session.expire_all()
    assert _db.session.query(RecordingImage).filter_by(recording_id=rec.id).count() == 2
    assert _db.session.get(RecordingImage, other["id"]).is_primary is False


def test_copy_survives_artist_image_deletion(app, seeded_ids, tmp_path):
    rec, client = _setup(app, tmp_path, "survive")
    art = _upload(client, f"/api/artists/{rec.performance.artist_id}/images")
    copy = client.post(f"/api/recordings/{rec.id}/images/from-artist").get_json()
    assert client.delete(f"/api/artists/images/{art['id']}").status_code == 200
    assert client.get(f"/api/recordings/images/{copy['id']}").status_code == 200


def test_use_artist_image_404_without_artist_image(app, seeded_ids, tmp_path):
    rec, client = _setup(app, tmp_path, "noart")
    assert client.post(f"/api/recordings/{rec.id}/images/from-artist").status_code == 404
    assert client.post("/api/recordings/999999/images/from-artist").status_code == 404


# ── POST /api/recordings/<id>/images/from-venue ────────────────────────────

def test_use_venue_image_copies_and_is_idempotent(app, seeded_ids, tmp_path):
    from app.models.venue import Venue
    rec, client = _setup(app, tmp_path, "venue")
    venue = Venue(name="Starr Hill Music Hall")
    _db.session.add(venue)
    _db.session.flush()
    rec.performance.venue_id = venue.id
    _db.session.commit()
    vimg = _upload(client, f"/api/venues/{venue.id}/images", _PNG, "v.png")
    assert client.get(f"/api/performances/{rec.performance_id}").get_json()["venue_image_id"] == vimg["id"]

    first = client.post(f"/api/recordings/{rec.id}/images/from-venue").get_json()
    assert first["is_primary"] is True and first["origin"] == "venue"
    again = client.post(f"/api/recordings/{rec.id}/images/from-venue").get_json()
    assert again["id"] == first["id"]
    _db.session.expire_all()
    assert _db.session.query(RecordingImage).filter_by(recording_id=rec.id).count() == 1


def test_use_venue_image_404_without_venue_image(app, seeded_ids, tmp_path):
    rec, client = _setup(app, tmp_path, "novenue")
    assert client.post(f"/api/recordings/{rec.id}/images/from-venue").status_code == 404


# -- Link three: the venue's image ------------------------------------------

def _with_venue(rec, name="Starr Hill Music Hall"):
    from app.models.venue import Venue
    venue = _db.session.query(Venue).filter_by(name=name).first()
    if venue is None:
        venue = Venue(name=name)
        _db.session.add(venue)
        _db.session.flush()
    rec.performance.venue_id = venue.id
    _db.session.commit()
    return venue


def test_venue_image_is_last_link_before_initials(app, seeded_ids, tmp_path):
    rec, client = _setup(app, tmp_path, "vchain")
    venue = _with_venue(rec)
    vimg = _upload(client, f"/api/venues/{venue.id}/images", _PNG, "v.png")
    want = f"/api/venues/images/{vimg['id']}"
    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert recording_row(rec)["image_url"] == want
    assert batch_recording_image_urls([rec.id]) == {rec.id: want}

    # The artist's image outranks the venue's.
    aimg = _upload(client, f"/api/artists/{rec.performance.artist_id}/images")
    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    want_a = f"/api/artists/images/{aimg['id']}"
    assert recording_row(rec)["image_url"] == want_a
    assert batch_recording_image_urls([rec.id]) == {rec.id: want_a}


def test_peers_never_get_a_venue_url(app, seeded_ids, tmp_path):
    from app.api.share import _peer_image_urls, _peer_row
    rec, client = _setup(app, tmp_path, "vpeer")
    venue = _with_venue(rec)
    _upload(client, f"/api/venues/{venue.id}/images", _PNG, "v.png")
    _db.session.expire_all()
    rec = _db.session.get(Recording, rec.id)
    assert _peer_row(rec)["image_url"] is None
    assert _peer_image_urls([rec]) == {}


def test_search_index_falls_back_to_venue(app, seeded_ids, tmp_path):
    from app.api.search import build_search_index
    rec, client = _setup(app, tmp_path, "vsearch")
    venue = _with_venue(rec)
    vimg = _upload(client, f"/api/venues/{venue.id}/images", _PNG, "v.png")
    row = next(r for r in build_search_index()["recordings"] if r["id"] == rec.id)
    assert row["image_url"] == f"/api/venues/images/{vimg['id']}"
