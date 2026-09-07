"""
tests/test_performer_media.py — Performer profile picture + Dossier
(2026-07-22): migration idempotency, image upload/serve/delete endpoints, and
the shared AI-cost-computation helper both this feature and ingest-side AI
Assist depend on.
"""

import sqlite3
from io import BytesIO

import pytest

from app.models.performer import Performer


@pytest.fixture()
def api(app):
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


# ── Migration ────────────────────────────────────────────────────────────────

def test_migrate_add_performer_image_dossier_idempotent(tmp_path):
    """Runs the migration script twice against a bare `performer` table (no
    image_ext/dossier_json yet) — first run adds both columns, second run is
    a no-op, neither run errors."""
    from scripts import migrate_add_performer_image_dossier as mod

    db_path = tmp_path / "legacy.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE performer (id INTEGER PRIMARY KEY, name TEXT)")
    con.commit()
    con.close()

    original_db = mod.DB
    try:
        mod.DB = str(db_path)
        mod.main()
        mod.main()   # idempotent — must not raise (e.g. "duplicate column")
    finally:
        mod.DB = original_db

    con = sqlite3.connect(str(db_path))
    cols = [r[1] for r in con.execute("PRAGMA table_info(performer)")]
    con.close()
    assert "image_ext" in cols
    assert "dossier_json" in cols


# ── Profile pictures — multi-image (rewritten 2026-08-07) ───────────────────
# The singular /image endpoints were replaced by /images (plural, keyed by
# image id) when a Performer gained multiple photos with one flagged primary.

def test_upload_get_delete_performer_image(api, app, seeded_ids, tmp_path):
    app.config["LIBRARY_ROOT"] = str(tmp_path)
    pid = seeded_ids["performer_id"]   # "Bill Evans", per conftest._seed()

    # No image yet.
    assert api.get(f"/api/performers/{pid}").get_json()["has_image"] is False
    assert api.get(f"/api/performers/{pid}/images").get_json() == []

    # Upload a jpg. The FIRST image becomes primary automatically — a fresh
    # upload must not leave the card faceless pending a second click.
    r = api.post(f"/api/performers/{pid}/images",
                 data={"image": (BytesIO(b"\xff\xd8\xff fake jpeg bytes"), "photo.jpg")},
                 content_type="multipart/form-data")
    assert r.status_code == 200
    img = r.get_json()["images"][0]
    assert img["is_primary"] is True
    assert img["origin"] == "upload"

    images_dir = tmp_path / "Bill Evans" / "_images"
    assert len(list(images_dir.glob("img_*.jpg"))) == 1
    assert api.get(f"/api/performers/{pid}").get_json()["has_image"] is True

    r = api.get(f"/api/performers/images/{img['id']}")
    assert r.status_code == 200
    assert r.mimetype == "image/jpeg"

    # Delete removes both row and file.
    r = api.delete(f"/api/performers/images/{img['id']}")
    assert r.status_code == 200
    assert list(images_dir.glob("img_*")) == []
    assert api.get(f"/api/performers/{pid}").get_json()["has_image"] is False
    assert api.get(f"/api/performers/images/{img['id']}").status_code == 404


def test_multiple_images_one_primary_and_promotion_on_delete(api, app, seeded_ids, tmp_path):
    """Several images coexist, exactly one is primary, and deleting the primary
    promotes a survivor rather than leaving the performer primary-less."""
    app.config["LIBRARY_ROOT"] = str(tmp_path)
    pid = seeded_ids["performer_id"]

    r = api.post(f"/api/performers/{pid}/images", content_type="multipart/form-data",
                 data={"image": [(BytesIO(b"a"), "one.jpg"),
                                 (BytesIO(b"b"), "two.png"),
                                 (BytesIO(b"c"), "three.webp")]})
    assert r.status_code == 200
    assert len(r.get_json()["images"]) == 3

    imgs = api.get(f"/api/performers/{pid}/images").get_json()
    assert len(imgs) == 3
    assert sum(1 for i in imgs if i["is_primary"]) == 1
    assert imgs[0]["is_primary"] is True    # ordered primary-first

    # Promote the third; the old primary must be cleared in the same act.
    third = imgs[2]
    assert api.post(f"/api/performers/images/{third['id']}/primary").status_code == 200
    imgs = api.get(f"/api/performers/{pid}/images").get_json()
    assert sum(1 for i in imgs if i["is_primary"]) == 1
    assert imgs[0]["id"] == third["id"]

    # Deleting the primary promotes a survivor.
    assert api.delete(f"/api/performers/images/{third['id']}").status_code == 200
    imgs = api.get(f"/api/performers/{pid}/images").get_json()
    assert len(imgs) == 2
    assert sum(1 for i in imgs if i["is_primary"]) == 1


def test_upload_rejects_unsupported_extension(api, app, seeded_ids, tmp_path):
    app.config["LIBRARY_ROOT"] = str(tmp_path)
    pid = seeded_ids["performer_id"]
    r = api.post(f"/api/performers/{pid}/images",
                 data={"image": (BytesIO(b"nope"), "notes.txt")},
                 content_type="multipart/form-data")
    assert r.status_code == 400
    assert api.get(f"/api/performers/{pid}/images").get_json() == []


def test_partial_upload_lands_good_files_and_reports_bad(api, app, seeded_ids, tmp_path):
    """A drop of 3 photos where 1 is unsupported must land the other 2 — a
    partial success is a 200 with an `errors` list, not a blanket 400."""
    app.config["LIBRARY_ROOT"] = str(tmp_path)
    pid = seeded_ids["performer_id"]
    r = api.post(f"/api/performers/{pid}/images", content_type="multipart/form-data",
                 data={"image": [(BytesIO(b"a"), "ok.jpg"),
                                 (BytesIO(b"b"), "bad.heic"),
                                 (BytesIO(b"c"), "ok2.png")]})
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["images"]) == 2
    assert len(body["errors"]) == 1
    assert "heic" in body["errors"][0]


def _db_performer(pid):
    from app.extensions import db as _db
    return _db.session.get(Performer, pid)


# ── Shared AI usage helper (ai_assist.py, reused by performer_research.py) ──
# Currency was removed 2026-09-07 — these assert TOKENS AND SEARCHES, and the
# absence of any priced field is part of what they check. A `cost_cents` key
# reappearing here is a regression, not a bonus.

def test_usage_summary_reports_tokens_not_currency():
    from types import SimpleNamespace
    from app.utils.ai_assist import _usage_summary

    usage = SimpleNamespace(
        input_tokens=5000, output_tokens=1200,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=3),
    )
    got = _usage_summary(usage)
    assert got["input_tokens"] == 5000
    assert got["output_tokens"] == 1200
    assert got["total_tokens"] == 6200
    assert got["web_search_requests"] == 3
    assert "cost_cents" not in got

    # Cache counts are kept SEPARATE from plain input on purpose — folding them
    # in would make a cached turn look like a full-price one.
    cached = _usage_summary(SimpleNamespace(
        input_tokens=800, output_tokens=200,
        cache_read_input_tokens=60000, cache_creation_input_tokens=0,
        server_tool_use=None))
    assert cached["cache_read_input_tokens"] == 60000
    assert cached["input_tokens"] == 800
    assert cached["web_search_requests"] == 0

    # No usage object at all is "not measured", which is not the same as zero.
    assert _usage_summary(None) is None


def test_estimate_tokens_is_a_range_that_grows_with_searches():
    from app.utils.ai_assist import estimate_tokens, MAX_SEARCHES, MAX_SEARCHES_WITH_QUESTION

    low, high = estimate_tokens()
    assert low < high
    # The ceiling is the search cap, which is a real bound rather than a guess.
    assert high == MAX_SEARCHES * 15_000 + 1_500

    # A run carrying a human question is allowed to dig further, so its ceiling
    # is higher. If these two ever match, the question is buying nothing.
    assert estimate_tokens(MAX_SEARCHES_WITH_QUESTION)[1] > high
