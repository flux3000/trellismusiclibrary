"""
tests/test_commons.py — Wikidata → Wikimedia Commons photo fetch (2026-08-07).

Network-free, like test_musicbrainz.py. What matters here is the LICENCE GATE
and the URL/QID plumbing, both pure functions over canned API payloads.

The licence gate is the part worth guarding with tests: it's the difference
between a library of freely-redistributable photos and a copyright problem that
only surfaces once peer sharing exposes the box.
"""

import json
import pytest

from app.utils import commons


@pytest.fixture()
def api(app):
    """Test client with auth disabled. Defined per-file, matching the existing
    convention in test_db_logic.py / test_artist_media.py."""
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


# ── QID extraction ──────────────────────────────────────────────────────────

def test_qid_from_links_finds_wikidata():
    links = {"Wikipedia": "https://en.wikipedia.org/wiki/The_Meters",
             "Wikidata":  "https://www.wikidata.org/wiki/Q1138832"}
    assert commons.qid_from_links(links) == "Q1138832"


def test_qid_from_links_handles_entity_url_shape():
    assert commons.qid_from_links(
        {"Wikidata": "http://www.wikidata.org/entity/Q42"}) == "Q42"


def test_qid_from_links_none_without_wikidata():
    assert commons.qid_from_links({"Discogs": "https://discogs.com/artist/1"}) is None
    assert commons.qid_from_links({}) is None
    assert commons.qid_from_links(None) is None


# ── Licence gate ────────────────────────────────────────────────────────────

def _payload(license_code, short="CC BY-SA 4.0", author="<a href='#'>Jane Doe</a>"):
    return {"query": {"pages": {"1": {"imageinfo": [{
        "url": "https://upload.wikimedia.org/x/Meters.jpg",
        "thumburl": "https://upload.wikimedia.org/thumb/Meters.jpg/900px.jpg",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Meters.jpg",
        "mime": "image/jpeg",
        "extmetadata": {
            "License":          {"value": license_code},
            "LicenseShortName": {"value": short},
            "Artist":           {"value": author},
        },
    }]}}}}


@pytest.mark.parametrize("code", ["cc-by-sa-4.0", "cc-by-3.0", "cc0", "pd"])
def test_free_licences_accepted(monkeypatch, code):
    monkeypatch.setattr(commons, "_get_json", lambda url: _payload(code))
    info = commons.file_info("Meters.jpg")
    assert info is not None
    assert info["url"].endswith("900px.jpg")      # thumbnail preferred
    assert info["author"] == "Jane Doe"           # HTML stripped
    assert "Wikimedia Commons" in info["credit"]


@pytest.mark.parametrize("code", ["cc-by-nc-4.0", "cc-by-nd-3.0",
                                  "cc-by-nc-sa-2.0", "fairuse", "non-free"])
def test_non_free_licences_rejected(monkeypatch, code):
    """Commons shouldn't host these at all — but 'shouldn't' is not an audit,
    and an image we can't redistribute must never enter the library."""
    monkeypatch.setattr(commons, "_get_json", lambda url: _payload(code))
    assert commons.file_info("Meters.jpg") is None


def test_missing_licence_metadata_rejected(monkeypatch):
    """An unattributable image is worse than no image: CC BY and BY-SA both
    require credit, and we cannot invent it."""
    payload = _payload("cc-by-4.0")
    payload["query"]["pages"]["1"]["imageinfo"][0]["extmetadata"] = {}
    monkeypatch.setattr(commons, "_get_json", lambda url: payload)
    assert commons.file_info("Meters.jpg") is None


def test_file_info_none_on_failed_fetch(monkeypatch):
    monkeypatch.setattr(commons, "_get_json", lambda url: None)
    assert commons.file_info("Meters.jpg") is None


def test_file_info_none_for_empty_filename():
    assert commons.file_info(None) is None
    assert commons.file_info("") is None


# ── P18 claim parsing ───────────────────────────────────────────────────────

def test_image_filenames_from_p18(monkeypatch):
    monkeypatch.setattr(commons, "_get_json", lambda url: {
        "entities": {"Q1": {"claims": {
            "P18": [{"mainsnak": {"datavalue": {"value": "The Meters 1974.jpg"}}}]}}}})
    assert commons.image_filenames_for_qid("Q1") == ["The Meters 1974.jpg"]


def test_uses_wbgetentities_not_wbgetclaims(monkeypatch):
    """Regression guard for 2026-08-08.

    wbgetclaims' `property` parameter takes ONE property id. Asking it for
    "P18|P373" failed validation, and MediaWiki reports that as HTTP 200 with an
    `error` body — which read as "no images" for every artist in the library.
    Two properties means wbgetentities.
    """
    seen = {}
    def fake(url):
        seen["url"] = url
        return {"entities": {"Q1": {"claims": {}}}}
    monkeypatch.setattr(commons, "_get_json", fake)
    commons.image_filenames_for_qid("Q1")
    assert "wbgetentities" in seen["url"]
    assert "wbgetclaims" not in seen["url"]


def test_api_error_body_is_not_treated_as_data(monkeypatch):
    """MediaWiki returns 200 with an `error` key for bad parameters. Reading
    that as an empty result is what made a broken request indistinguishable
    from an artist with no photo."""
    import urllib.request

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return b'{"error":{"code":"param-illegal","info":"bad property"}}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResp())
    monkeypatch.setattr(commons, "_throttle", lambda: None)
    assert commons._get_json("http://x") is None


def test_image_filenames_include_category_after_p18(monkeypatch):
    """P18 ranks first; the Commons category supplies the SECOND click.

    P18 is usually a single photo, so without the category fallback "find me
    another one" would have nowhere to go.
    """
    def fake(url):
        if "wbgetentities" in url:
            return {"entities": {"Q1": {"claims": {
                "P18":  [{"mainsnak": {"datavalue": {"value": "Main.jpg"}}}],
                "P373": [{"mainsnak": {"datavalue": {"value": "The Meters"}}}]}}}}
        return {"query": {"categorymembers": [
            {"title": "File:Main.jpg"},          # dupe of P18 — must collapse
            {"title": "File:Live 1976.jpg"},
            {"title": "File:Poster.pdf"},        # not an image — dropped
        ]}}
    monkeypatch.setattr(commons, "_get_json", fake)
    assert commons.image_filenames_for_qid("Q1") == ["Main.jpg", "Live 1976.jpg"]


def test_image_filenames_empty_when_entity_has_no_image(monkeypatch):
    """Very common — most Wikidata entities carry no P18 at all."""
    monkeypatch.setattr(commons, "_get_json",
                        lambda url: {"entities": {"Q1": {"claims": {}}}})
    assert commons.image_filenames_for_qid("Q1") == []


def test_image_filenames_empty_for_missing_qid():
    assert commons.image_filenames_for_qid(None) == []


def test_find_photo_skips_already_imported(monkeypatch, app, seeded_ids):
    """A second click must return a DIFFERENT photo, not the same one again."""
    from app.extensions import db as _db
    from app.models.artist import Artist

    p = _db.session.get(Artist, seeded_ids["artist_id"])
    p.mb_links_json = json.dumps({"Wikidata": "https://www.wikidata.org/wiki/Q1"})

    monkeypatch.setattr(commons, "image_filenames_for_qid",
                        lambda qid: ["First.jpg", "Second.jpg"])
    monkeypatch.setattr(commons, "file_info",
                        lambda f: {"url": "http://x/" + f, "credit": "c",
                                   "licence": "CC BY", "descurl": "http://d"})
    monkeypatch.setattr(commons, "download", lambda url: (b"bytes", ".jpg"))

    first = commons.find_photo_for_artist(p)
    assert first["source_ref"] == "First.jpg"

    second = commons.find_photo_for_artist(p, exclude={"First.jpg"})
    assert second["source_ref"] == "Second.jpg"

    # Nothing left — the ordinary outcome once an act's free images run out.
    assert commons.find_photo_for_artist(
        p, exclude={"First.jpg", "Second.jpg"}) is None


# ── Whole chain ─────────────────────────────────────────────────────────────

def test_find_photo_returns_none_without_wikidata_link(app, seeded_ids):
    """No MusicBrainz match means no Wikidata link means nothing to follow —
    the ordinary case for most of a long-tail library."""
    from app.extensions import db as _db
    from app.models.artist import Artist

    p = _db.session.get(Artist, seeded_ids["artist_id"])
    p.mb_links_json = json.dumps({"Discogs": "https://discogs.com/artist/1"})
    assert commons.find_photo_for_artist(p) is None


def test_find_photo_tolerates_malformed_links_json(app, seeded_ids):
    from app.extensions import db as _db
    from app.models.artist import Artist

    p = _db.session.get(Artist, seeded_ids["artist_id"])
    p.mb_links_json = "{not valid json"
    assert commons.find_photo_for_artist(p) is None


def test_fetch_endpoint_requires_musicbrainz_match(api, seeded_ids):
    """The Wikidata link comes from MusicBrainz, so an unmatched act has
    nothing to follow — a clear 400 explaining the prerequisite, not a 500."""
    r = api.post(f"/api/artists/{seeded_ids['artist_id']}/images/fetch")
    assert r.status_code == 400
    assert "MusicBrainz" in r.get_json()["error"]
