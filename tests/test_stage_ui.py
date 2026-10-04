"""
tests/test_stage_ui.py -- Stage (Performance.stage) as an optional field beside Event and Venue
(Resolver v2, chunk 5). The backend half is covered in test_resolve_event_stage.py; this file holds
the performances-API round trip, the peer payload and the JS wiring that has no browser test.
"""

import re
from pathlib import Path

import pytest

from app.extensions import db as _db
from app.models.performance import Performance

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")


@pytest.fixture()
def api(app):
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


def test_performances_api_round_trips_stage(api, seeded_ids):
    pid = seeded_ids["performance_id"]
    assert api.get(f"/api/performances/{pid}").get_json()["stage"] is None
    assert api.put(f"/api/performances/{pid}", json={"stage": "  Harbor Stage "}).status_code == 200
    assert api.get(f"/api/performances/{pid}").get_json()["stage"] == "Harbor Stage"
    assert api.put(f"/api/performances/{pid}", json={"stage": ""}).status_code == 200
    assert api.get(f"/api/performances/{pid}").get_json()["stage"] is None
    assert _db.session.get(Performance, pid).stage is None


def test_peer_performance_payload_carries_stage_beside_event():
    src = (ROOT / "app/api/share.py").read_text(encoding="utf-8")
    m = re.search(r'"stage":\s+p\.stage,.*?"event_name":\s+p\.event\.name', src, re.S)
    assert m, "peer performance_detail must serve stage next to event_name"



def test_view_recording_stage_is_editable_only_through_the_shared_helper():
    assert "makeInlineEditable(document.getElementById('rec-f-stage')" in APP_JS
    # Playback mode (canEdit false): rendered as plain text, and only when a stage exists.
    assert "(stageStr ? `<span class=\"rec-dot\">·</span><span class=\"rec-f-loc\">${esc(stageStr)}</span>` : '')" in APP_JS
    # Edit mode: the editable span is gated by the same canEdit as Event.
    assert re.search(r"\$\{canEdit\s*\n\s*\? `<span class=\"rec-dot\">·</span><span class=\"rec-f rec-f-stage pp-editable", APP_JS)


def test_stage_is_wired_on_every_ingest_surface():
    for needle in ("id=\"f-stage\"", "f.stage           = document.getElementById('f-stage')",
                   "f.stage           = rv('stage')", "key: 'stage',     form: 'stage'",
                   "id=\"lq-apply-stage\"", "['lq-apply-stage', 'stage']",
                   "stage: 'stage',"):
        assert needle in APP_JS, needle
    assert "'event_name', 'stage'," in APP_JS            # dirty-check snapshot
