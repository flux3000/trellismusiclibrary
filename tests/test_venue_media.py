"""
tests/test_venue_media.py — Venue photos (2026-08-07).

Venue images are a PARALLEL table to artist_image with SHARED behaviour
(app/utils/entity_images.py). These tests exist to prove the sharing actually
holds: every assertion here has a twin in test_artist_media.py, and if the
two ever diverge it means the shared helpers stopped being shared.
"""

from io import BytesIO

import pytest

from app.models.venue import Venue


@pytest.fixture()
def api(app):
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


@pytest.fixture()
def venue(app):
    from app.extensions import db as _db
    v = Venue(name="The Fillmore", city="San Francisco", state="CA")
    _db.session.add(v)
    _db.session.commit()
    return v


def test_upload_list_serve_delete(api, app, venue, tmp_path):
    app.config["LIBRARY_ROOT"] = str(tmp_path)

    assert api.get(f"/api/venues/{venue.id}").get_json()["has_image"] is False
    assert api.get(f"/api/venues/{venue.id}/images").get_json() == []

    # First image becomes primary automatically — a fresh upload must not leave
    # the page faceless pending a second click.
    r = api.post(f"/api/venues/{venue.id}/images",
                 data={"image": (BytesIO(b"\xff\xd8\xff jpeg"), "hall.jpg")},
                 content_type="multipart/form-data")
    assert r.status_code == 200
    img = r.get_json()["images"][0]
    assert img["is_primary"] is True
    assert img["origin"] == "upload"
    assert img["url"] == f"/api/venues/images/{img['id']}"

    # Files land under _venues/, NOT alongside artist photos — a venue and
    # an act can share a name ("Fillmore") and must not collide.
    images_dir = tmp_path / "_venues" / "The Fillmore" / "_images"
    assert len(list(images_dir.glob("img_*.jpg"))) == 1

    assert api.get(f"/api/venues/{venue.id}").get_json()["has_image"] is True
    assert api.get(f"/api/venues/images/{img['id']}").mimetype == "image/jpeg"

    assert api.delete(f"/api/venues/images/{img['id']}").status_code == 200
    assert list(images_dir.glob("img_*")) == []
    assert api.get(f"/api/venues/images/{img['id']}").status_code == 404


def test_one_primary_and_promotion_on_delete(api, app, venue, tmp_path):
    """Exactly one primary at all times, and deleting it promotes a survivor."""
    app.config["LIBRARY_ROOT"] = str(tmp_path)
    r = api.post(f"/api/venues/{venue.id}/images", content_type="multipart/form-data",
                 data={"image": [(BytesIO(b"a"), "a.jpg"),
                                 (BytesIO(b"b"), "b.png"),
                                 (BytesIO(b"c"), "c.webp")]})
    assert r.status_code == 200
    imgs = api.get(f"/api/venues/{venue.id}/images").get_json()
    assert len(imgs) == 3
    assert sum(1 for i in imgs if i["is_primary"]) == 1
    assert imgs[0]["is_primary"] is True          # ordered primary-first

    third = imgs[2]
    assert api.post(f"/api/venues/images/{third['id']}/primary").status_code == 200
    imgs = api.get(f"/api/venues/{venue.id}/images").get_json()
    assert sum(1 for i in imgs if i["is_primary"]) == 1
    assert imgs[0]["id"] == third["id"]

    assert api.delete(f"/api/venues/images/{third['id']}").status_code == 200
    imgs = api.get(f"/api/venues/{venue.id}/images").get_json()
    assert len(imgs) == 2
    assert sum(1 for i in imgs if i["is_primary"]) == 1


def test_partial_upload_lands_good_files(api, app, venue, tmp_path):
    """A drop where one file is unsupported must land the rest — 200 with an
    `errors` list, not a blanket 400."""
    app.config["LIBRARY_ROOT"] = str(tmp_path)
    r = api.post(f"/api/venues/{venue.id}/images", content_type="multipart/form-data",
                 data={"image": [(BytesIO(b"a"), "ok.jpg"),
                                 (BytesIO(b"b"), "bad.heic")]})
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["images"]) == 1
    assert len(body["errors"]) == 1
    assert "heic" in body["errors"][0]


def test_upload_rejects_unsupported_only(api, app, venue, tmp_path):
    app.config["LIBRARY_ROOT"] = str(tmp_path)
    r = api.post(f"/api/venues/{venue.id}/images",
                 data={"image": (BytesIO(b"x"), "notes.txt")},
                 content_type="multipart/form-data")
    assert r.status_code == 400
    assert api.get(f"/api/venues/{venue.id}/images").get_json() == []


def test_deleting_venue_cascades_to_images(api, app, venue, tmp_path):
    """cascade='all, delete-orphan' on Venue.images — an orphaned image row
    would point at a venue that no longer exists, and FK enforcement is ON."""
    from app.extensions import db as _db
    from app.models.venue_image import VenueImage

    app.config["LIBRARY_ROOT"] = str(tmp_path)
    api.post(f"/api/venues/{venue.id}/images",
             data={"image": (BytesIO(b"a"), "a.jpg")},
             content_type="multipart/form-data")
    assert _db.session.query(VenueImage).count() == 1

    _db.session.delete(_db.session.get(Venue, venue.id))
    _db.session.commit()
    assert _db.session.query(VenueImage).count() == 0


def test_shared_helpers_work_on_both_models():
    """set_primary()/primary_for() are model-agnostic via __parent_fk__ — the
    single hook that lets one implementation serve both tables."""
    from app.models.artist_image import ArtistImage
    from app.models.venue_image import VenueImage

    assert ArtistImage.__parent_fk__ == "artist_id"
    assert VenueImage.__parent_fk__ == "venue_id"
    # Same column surface, so the shared endpoint bodies can't be surprised.
    shared = {"filename", "ext", "is_primary", "sort_order", "origin",
              "caption", "credit", "source_ref", "created_at"}
    for model in (ArtistImage, VenueImage):
        assert shared <= set(model.__table__.columns.keys()), model
