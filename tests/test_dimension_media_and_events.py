"""
tests/test_dimension_media_and_events.py — Artist + Event photos, and the Event
dimension's newly-complete CRUD (2026-09-01).

Two things are under test here, and they share a file because they landed
together:

1. ARTIST AND EVENT PHOTOS. These are the third and fourth parallel image
   tables. Every assertion below has a twin in test_venue_media.py and
   test_performer_media.py — deliberately, because the whole argument for
   parallel tables is that the BEHAVIOUR is shared. If these two files ever
   disagree it means the sharing stopped being real.

   The routes are now GENERATED (ei.register_image_routes) rather than written
   per blueprint, so there is a second thing worth pinning: that the generated
   set has the same shape, the same URLs and the same endpoint names the
   hand-written ones had.

2. EVENT CRUD. Event had no delete and a detail endpoint that raised
   AttributeError the moment a performance was attached to it (`p.artist`,
   a survivor of the 2026-07-11 Performer remodel). Nothing caught it because
   nothing called it — there was no Event page. There is one now.
"""

from io import BytesIO

import pytest

from app.models.artist import Artist
from app.models.event import Event


@pytest.fixture()
def api(app):
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


@pytest.fixture()
def person(app):
    from app.extensions import db as _db
    a = Artist(name="Danny Gatton")
    _db.session.add(a)
    _db.session.commit()
    return a


@pytest.fixture()
def event(app):
    from app.extensions import db as _db
    e = Event(name="Bonnaroo 2009", city="Manchester", state="TN",
              start_year=2009, start_month=6, start_day=11)
    _db.session.add(e)
    _db.session.commit()
    return e


# ── Photos: Artist ───────────────────────────────────────────────────────────

def test_artist_upload_list_serve_delete(api, app, person, tmp_path):
    app.config["LIBRARY_ROOT"] = str(tmp_path)

    assert api.get(f"/api/artists/{person.id}").get_json()["has_image"] is False
    assert api.get(f"/api/artists/{person.id}/images").get_json() == []

    r = api.post(f"/api/artists/{person.id}/images",
                 data={"image": (BytesIO(b"\xff\xd8\xff jpeg"), "danny.jpg")},
                 content_type="multipart/form-data")
    assert r.status_code == 200
    img = r.get_json()["images"][0]
    assert img["is_primary"] is True                    # first one, automatically
    assert img["url"] == f"/api/artists/images/{img['id']}"

    # The `_artists` bucket is the point: a PERSON and an ACT share a name
    # constantly in this corpus, and performer photos live at the library root
    # with no prefix at all, so without it they would write to one folder.
    images_dir = tmp_path / "_artists" / "Danny Gatton" / "_images"
    assert len(list(images_dir.glob("img_*.jpg"))) == 1
    assert not (tmp_path / "Danny Gatton" / "_images").exists()

    assert api.get(f"/api/artists/{person.id}").get_json()["has_image"] is True
    assert api.get(f"/api/artists/images/{img['id']}").mimetype == "image/jpeg"

    assert api.delete(f"/api/artists/images/{img['id']}").status_code == 200
    assert list(images_dir.glob("img_*")) == []
    assert api.get(f"/api/artists/images/{img['id']}").status_code == 404


def test_artist_one_primary_and_promotion_on_delete(api, app, person, tmp_path):
    app.config["LIBRARY_ROOT"] = str(tmp_path)
    api.post(f"/api/artists/{person.id}/images", content_type="multipart/form-data",
             data={"image": [(BytesIO(b"a"), "a.jpg"),
                             (BytesIO(b"b"), "b.png"),
                             (BytesIO(b"c"), "c.webp")]})
    imgs = api.get(f"/api/artists/{person.id}/images").get_json()
    assert len(imgs) == 3
    assert sum(1 for i in imgs if i["is_primary"]) == 1
    assert imgs[0]["is_primary"] is True                # ordered primary-first

    third = imgs[2]
    assert api.post(f"/api/artists/images/{third['id']}/primary").status_code == 200
    imgs = api.get(f"/api/artists/{person.id}/images").get_json()
    assert imgs[0]["id"] == third["id"]
    assert sum(1 for i in imgs if i["is_primary"]) == 1

    assert api.delete(f"/api/artists/images/{third['id']}").status_code == 200
    imgs = api.get(f"/api/artists/{person.id}/images").get_json()
    assert len(imgs) == 2
    assert sum(1 for i in imgs if i["is_primary"]) == 1  # a survivor was promoted


def test_artist_partial_upload_lands_good_files(api, app, person, tmp_path):
    app.config["LIBRARY_ROOT"] = str(tmp_path)
    r = api.post(f"/api/artists/{person.id}/images", content_type="multipart/form-data",
                 data={"image": [(BytesIO(b"a"), "ok.jpg"),
                                 (BytesIO(b"b"), "bad.heic")]})
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["images"]) == 1 and len(body["errors"]) == 1


def test_deleting_artist_cascades_to_images(api, app, person, tmp_path):
    from app.extensions import db as _db
    from app.models.artist_image import ArtistImage

    app.config["LIBRARY_ROOT"] = str(tmp_path)
    api.post(f"/api/artists/{person.id}/images",
             data={"image": (BytesIO(b"a"), "a.jpg")},
             content_type="multipart/form-data")
    assert _db.session.query(ArtistImage).count() == 1

    _db.session.delete(_db.session.get(Artist, person.id))
    _db.session.commit()
    assert _db.session.query(ArtistImage).count() == 0


# ── Photos: Event ────────────────────────────────────────────────────────────

def test_event_upload_list_serve_delete(api, app, event, tmp_path):
    app.config["LIBRARY_ROOT"] = str(tmp_path)

    assert api.get(f"/api/events/{event.id}").get_json()["has_image"] is False

    r = api.post(f"/api/events/{event.id}/images",
                 data={"image": (BytesIO(b"\xff\xd8\xff jpeg"), "gates.jpg")},
                 content_type="multipart/form-data")
    assert r.status_code == 200
    img = r.get_json()["images"][0]
    assert img["is_primary"] is True
    assert img["url"] == f"/api/events/images/{img['id']}"

    images_dir = tmp_path / "_events" / "Bonnaroo 2009" / "_images"
    assert len(list(images_dir.glob("img_*.jpg"))) == 1

    assert api.get(f"/api/events/images/{img['id']}").mimetype == "image/jpeg"
    assert api.delete(f"/api/events/images/{img['id']}").status_code == 200
    assert api.get(f"/api/events/images/{img['id']}").status_code == 404


def test_deleting_event_cascades_to_images(api, app, event, tmp_path):
    from app.extensions import db as _db
    from app.models.event_image import EventImage

    app.config["LIBRARY_ROOT"] = str(tmp_path)
    api.post(f"/api/events/{event.id}/images",
             data={"image": (BytesIO(b"a"), "a.jpg")},
             content_type="multipart/form-data")
    assert _db.session.query(EventImage).count() == 1

    _db.session.delete(_db.session.get(Event, event.id))
    _db.session.commit()
    assert _db.session.query(EventImage).count() == 0


# ── The four image tables must stay shape-compatible ─────────────────────────

def test_all_four_image_models_share_one_surface():
    """
    `__parent_fk__` is the ONLY thing the shared helpers are allowed to need.
    Every other column has to be present on all four, or handle_upload /
    handle_delete / image_payload will work on some tables and not others —
    which is the exact drift parallel tables are supposed to be worth risking.
    """
    from app.models.performer_image import PerformerImage
    from app.models.venue_image import VenueImage
    from app.models.artist_image import ArtistImage
    from app.models.event_image import EventImage

    assert PerformerImage.__parent_fk__ == "performer_id"
    assert VenueImage.__parent_fk__     == "venue_id"
    assert ArtistImage.__parent_fk__    == "artist_id"
    assert EventImage.__parent_fk__     == "event_id"

    shared = {"filename", "ext", "is_primary", "sort_order", "origin",
              "caption", "credit", "source_ref", "created_at"}
    for model in (PerformerImage, VenueImage, ArtistImage, EventImage):
        assert shared <= set(model.__table__.columns.keys()), model
        assert model.__parent_fk__ in model.__table__.columns, model


def test_generated_routes_match_the_handwritten_shape(app):
    """
    The registrar has to produce exactly the five URLs the hand-written venue
    routes produced, under the same endpoint names — url_for() callers and any
    existing test that names an endpoint must not care that they moved.
    """
    # One rule per METHOD, so collect the union rather than indexing by URL —
    # list and upload share a URL and would otherwise shadow each other.
    methods = {}
    for r in app.url_map.iter_rules():
        methods.setdefault(str(r.rule), set()).update(r.methods)

    for ns, kind in (("venues", "venue"), ("artists", "artist"), ("events", "event")):
        assert {"GET", "POST"} <= methods[f"/api/{ns}/<int:{kind}_id>/images"]
        assert {"GET", "DELETE"} <= methods[f"/api/{ns}/images/<int:image_id>"]
        assert "POST" in methods[f"/api/{ns}/images/<int:image_id>/primary"]

    endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    for name in ("list_venue_images", "upload_venue_images", "serve_venue_image",
                 "make_venue_image_primary", "delete_venue_image"):
        assert f"venues.{name}" in endpoints, name


# ── Event CRUD ───────────────────────────────────────────────────────────────

def test_event_detail_with_a_performance_attached(api, app, event):
    """
    Regression: get_event() read `p.artist.name`, a name Performance has not
    had since the 2026-07-11 remodel. Any event with a performance on it
    raised AttributeError — invisible because nothing called the endpoint.
    """
    from app.extensions import db as _db
    from app.models.performance import Performance

    perf = _db.session.query(Performance).first()
    assert perf is not None, "conftest seeds one performance"
    perf.event_id = event.id
    _db.session.commit()

    r = api.get(f"/api/events/{event.id}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["performance_count"] == 1
    assert body["performances"][0]["performer"]         # a name, not an exception
    assert body["performances"][0]["performer_id"] == perf.performer_id
    # Recordings ride along so the page's default tab has something to draw.
    assert body["recording_count"] == len(perf.recordings)
    assert len(body["recordings"]) == len(perf.recordings)


def test_event_list_counts_and_search(api, app, event):
    from app.extensions import db as _db
    from app.models.performance import Performance

    perf = _db.session.query(Performance).first()
    perf.event_id = event.id
    _db.session.commit()

    rows = api.get("/api/events/").get_json()
    row = next(r for r in rows if r["id"] == event.id)
    assert row["performance_count"] == 1
    assert row["recording_count"] == len(perf.recordings)
    assert row["image_id"] is None

    # Name AND city both match — "everything from Manchester" is the same
    # question asked of an event as of a venue.
    assert [r["id"] for r in api.get("/api/events/?q=bonnaroo").get_json()] == [event.id]
    assert [r["id"] for r in api.get("/api/events/?q=manchester").get_json()] == [event.id]
    assert api.get("/api/events/?q=zzzz").get_json() == []


def test_event_delete_is_guarded_then_allowed(api, app, event):
    from app.extensions import db as _db
    from app.models.performance import Performance

    perf = _db.session.query(Performance).first()
    perf.event_id = event.id
    _db.session.commit()

    # `performance.event_id` is nullable, so cascading would orphan real shows
    # to remove a label. Same guard Venue, Artist and Genre deletes carry.
    r = api.delete(f"/api/events/{event.id}")
    assert r.status_code == 409
    assert "performance" in r.get_json()["error"]

    perf.event_id = None
    _db.session.commit()
    assert api.delete(f"/api/events/{event.id}").status_code == 200
    assert api.get(f"/api/events/{event.id}").status_code == 404


def test_event_partial_dates_survive_blank_input(api, app):
    """
    Dates arrive from text boxes, so '' turns up as often as '1989'. int('')
    raises — a cleared box must not 500 a save. A year with no month is a
    normal state for this corpus, not incomplete input.
    """
    r = api.post("/api/events/", json={"name": "Fall Tour 1989",
                                       "start_year": "1989", "start_month": "",
                                       "start_day": None, "end_year": "not a year"})
    assert r.status_code == 201
    eid = r.get_json()["id"]

    body = api.get(f"/api/events/{eid}").get_json()
    assert body["start_year"] == 1989
    assert body["start_month"] is None
    assert body["end_year"] is None

    assert api.put(f"/api/events/{eid}", json={"start_year": ""}).status_code == 200
    assert api.get(f"/api/events/{eid}").get_json()["start_year"] is None


def test_event_duplicate_name_is_409_with_the_existing_id(api, app, event):
    r = api.post("/api/events/", json={"name": "bonnaroo 2009"})
    assert r.status_code == 409
    assert r.get_json()["id"] == event.id


# ── Index-page payload contract ──────────────────────────────────────────────
#
# The five dimension index pages are built from the LIST endpoints, and every
# tile reads fields that were added for them (image_id, recording_count, the
# performer's genre_color). A missing field is not an error anywhere — it
# renders as a tile with no photo and no counts, which looks exactly like a
# dimension that genuinely has none. That is CONTEXT.md's standing trap: a
# failure disguised as an ordinary empty state. So the contract is pinned here
# rather than trusted.

def test_list_endpoints_carry_the_index_tile_fields(api, app, person, event):
    from app.extensions import db as _db
    from app.models.performance import Performance
    from app.models.genre import Genre

    perf = _db.session.query(Performance).first()
    perf.event_id = event.id
    # conftest seeds no genre — nothing in the base graph needs one — so this
    # test supplies its own rather than asserting against an empty list, which
    # would pass whatever the payload shape turned out to be.
    _db.session.add(Genre(name="Bluegrass", color="#7a8b99"))
    _db.session.commit()

    contract = {
        "/api/venues/":     {"id", "name", "city", "state", "country",
                             "performance_count", "recording_count", "image_id"},
        "/api/performers/": {"id", "name", "sort_name", "recording_count", "members",
                             "genre_id", "genre_name", "genre_color", "image_id"},
        "/api/artists/":    {"id", "name", "sort_name", "recording_count",
                             "performer_count", "image_id"},
        "/api/genres/":     {"id", "name", "description", "color",
                             "performer_count", "recording_count"},
        "/api/events/":     {"id", "name", "city", "state", "country", "venue_id",
                             "venue_name", "start_year", "end_year",
                             "performance_count", "recording_count", "image_id"},
    }
    for path, fields in contract.items():
        rows = api.get(path).get_json()
        assert rows, f"{path} returned nothing — the seed graph should cover it"
        assert fields <= set(rows[0]), f"{path} missing {fields - set(rows[0])}"


def test_performer_list_counts_do_not_multiply_with_photos(api, app, tmp_path):
    """
    recording_count comes from a GROUP BY over a join, and image_id from a
    separate grouped query. Joining the image table into the same statement
    would multiply the recording count by the number of photos — the classic
    two-aggregates-one-query bug, and one that reads as plausible data rather
    than as an error.
    """
    from io import BytesIO
    app.config["LIBRARY_ROOT"] = str(tmp_path)

    before = api.get("/api/performers/").get_json()[0]
    pid, expected = before["id"], before["recording_count"]
    assert expected > 0, "conftest seeds a recording"

    api.post(f"/api/performers/{pid}/images", content_type="multipart/form-data",
             data={"image": [(BytesIO(b"a"), "a.jpg"), (BytesIO(b"b"), "b.jpg")]})

    after = next(r for r in api.get("/api/performers/").get_json() if r["id"] == pid)
    assert after["recording_count"] == expected
    assert after["image_id"] is not None
