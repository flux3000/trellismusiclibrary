"""
Album form, second pass: no Info File tab for albums, Lomax (skill 'album') back on both pages, the
MusicBrainz tab on Add Recording, notes saved on confirm, and the pre-import release endpoints.
Server paths are exercised for real with MusicBrainz mocked; JS is checked by source, against the
keys the Python actually produces.
"""
import re
from pathlib import Path

from app.extensions import db as _db
from app.models.recording import Recording
from app.utils import bulk_ingest_run as bir
from app.utils import musicbrainz as mb

from tests.test_learned_aliases import env, _flac  # noqa: F401  (fixture and helper)
from tests.test_release_block_api import _SEARCH_RESPONSE, _LOOKUP_RESPONSE, _login_as

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
API_JS = (ROOT / "app/static/js/api.js").read_text(encoding="utf-8")


def _review():
    i = APP_JS.index("function renderIngestReview()")
    j = APP_JS.index("function fmtDur(", i)
    return APP_JS[i:j]


# ── server ──────────────────────────────────────────────────────────────────

def test_release_candidates_endpoint(app, monkeypatch, seeded_ids):
    client = app.test_client()
    _login_as(client)
    url = "/api/ingest/release-candidates?artist=Bill%20Evans&title=Head%20Hunters&tracks=1"
    assert mb.enabled() is False
    assert client.get(url).status_code == 503
    assert client.get("/api/ingest/release-candidates?artist=&title=").get_json() == {"candidates": []}
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "_get", lambda path, params: _SEARCH_RESPONSE)
    res = client.get(url)
    assert res.status_code == 200
    c = res.get_json()["candidates"]
    assert len(c) == 1 and c[0]["mbid"] == "rel-mbid-1"
    # Every key the picker reads is on the candidate.
    for k in ("mbid", "title", "label", "catalog_number", "country", "date"):
        assert k in c[0]


def test_release_detail_endpoint(app, monkeypatch, seeded_ids):
    client = app.test_client()
    _login_as(client)
    assert client.get("/api/ingest/release-detail?mbid=x").status_code == 503
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "_get", lambda path, params: _LOOKUP_RESPONSE)
    rel = client.get("/api/ingest/release-detail?mbid=rel-mbid-1").get_json()["release"]
    for k in ("mbid", "title", "label", "catalog_number", "country", "date", "tracks"):
        assert k in rel
    assert rel["tracks"][0]["title"] == "Chameleon"


def _studio_payload(env, **extra):
    _flac(env.lib / "Album" / "01.flac", ARTIST="Some Band", TITLE="One")
    _flac(env.lib / "Album" / "02.flac", ARTIST="Some Band", TITLE="Two")
    p = {
        "source_folder_path": str(env.lib / "Album"), "artist_name": "Some Band",
        "start_year": 1994, "start_month": None, "start_day": None, "venue_name": "",
        "kind": "studio", "title": "Second Record", "is_complete": True, "skip_analysis": True,
        "tracks": [{"track_number": 1, "title": "My Own", "filename": "01.flac"},
                   {"track_number": 2, "title": "Track 2", "filename": "02.flac"}],
    }
    p.update(extra)
    return p


def test_confirm_saves_notes(env):
    from app.api.ingest import _do_confirm
    out = _do_confirm(_studio_payload(env, notes="Recorded in a week."), bir._owner_user_id())
    assert _db.session.get(Recording, out["recording_id"]).notes == "Recorded in a week."


def test_confirm_with_release_links_and_fills_only_null(env, monkeypatch):
    from app.api.ingest import _do_confirm, _handle_mb_release
    release = {"mbid": "rel-9", "title": "Other Title", "release_group_id": "rg-9", "release_type": "Album",
               "label": "Columbia", "catalog_number": "KC 1", "country": "US", "date": "1973-10-13",
               "tracks": [{"position": 1, "title": "Release One"}, {"position": 2, "title": "Release Two"}]}
    monkeypatch.setattr(mb, "lookup_release", lambda mbid: release if mbid == "rel-9" else None)
    out = _do_confirm(_studio_payload(env, mb_release_id="rel-9"), bir._owner_user_id())
    rec = _db.session.get(Recording, out["recording_id"])
    assert rec.mb_release_status == "linked" and rec.mb_release_id == "rel-9"
    assert (rec.mb_label, rec.mb_catalog_number, rec.mb_release_country) == ("Columbia", "KC 1", "US")
    assert rec.title == "Second Record"                      # a title the person had is kept
    assert rec.performance.start_year == 1994                # so is the year
    titles = {t.track_number: t.title for t in rec.tracks}
    assert titles == {1: "My Own", 2: "Release Two"}         # "Track 2" counts as empty, a real title does not

    # The background follow-up leaves a set status alone.
    def boom(r):
        raise AssertionError("follow-up re-matched a linked release")
    monkeypatch.setattr(mb, "try_match_release", boom)
    _handle_mb_release(rec.id)


def test_confirm_release_lookup_failure_does_not_fail_save(env, monkeypatch):
    from app.api.ingest import _do_confirm
    monkeypatch.setattr(mb, "lookup_release", lambda mbid: None)
    out = _do_confirm(_studio_payload(env, mb_release_id="gone"), bir._owner_user_id())
    rec = _db.session.get(Recording, out["recording_id"])
    assert rec.mb_release_status is None and rec.mb_release_id is None


def test_live_confirm_ignores_release_id(env, monkeypatch):
    from app.api.ingest import _do_confirm
    monkeypatch.setattr(mb, "lookup_release", lambda mbid: (_ for _ in ()).throw(AssertionError("looked up")))
    out = _do_confirm(_studio_payload(env, kind="live", title=None, mb_release_id="rel-9"), bir._owner_user_id())
    assert _db.session.get(Recording, out["recording_id"]).mb_release_status is None


# ── JS ──────────────────────────────────────────────────────────────────────

def test_album_tab_set_on_both_pages():
    # Add Recording: no Info File, MusicBrainz tab, Lomax always.
    r = _review()
    assert "resolver: hasResolver && !studio, research: true, staged: false, info: !studio, mb: studio" in r
    assert 'id="isp-mb"' in r and 'id="ingest-mb-root"' in r
    assert "${hasResolver && !studio ?" in r                  # no Resolver pane for an album
    # View Recording: no Info File and no Resolver for a studio record, Lomax for an editor.
    assert "resolver: showResolver && !isStudioKind, research: canEdit, staged: stagedCount > 0, info: !isStudioKind" in APP_JS
    assert "${showResolver && !isStudioKind ?" in APP_JS
    # The tab list itself.
    i = APP_JS.index("const DETAILS_TABS")
    t = APP_JS[i:i + 1200]
    assert "['mb', 'MusicBrainz']" in t and "(k !== 'info' || info)" in t and "(k !== 'mb' || mb)" in t
    # Only the album layout shows the MusicBrainz tab.
    assert APP_JS.count("detailsTabsHtml(") == 3 and "mb: studio" in r and "mb: !isStudioKind" not in APP_JS
    assert APP_JS.count("'MusicBrainz'") == 1


def test_missing_tab_falls_back_to_first_remaining():
    assert "function pickPane(p)" in APP_JS and "panel.querySelector('.slide-tab')?.dataset.pane" in APP_JS
    assert "openPane(pickPane(state.recLastPane))" in APP_JS and "const startPane = pickPane(state.recLastPane)" in APP_JS
    r = _review()
    assert "_hasTab('isp-info') ? 'isp-info' : (panel.querySelector('.slide-tab')?.dataset.ipane" in r


def test_album_lomax_wiring():
    r = _review()
    assert "skill: ingest.kind === 'studio' ? 'album' : 'recording'" in r
    assert "current: ingest.kind === 'studio' ? collectAlbumMeta : collectCurrentMeta" in r
    # The album form fields a proposal fills, and the keys the album skill actually proposes.
    assert "{ title: 'f-album-title', year: 'f-year', notes: 'f-notes' }" in r
    from app.lomax.skills.album import FIELDS
    assert set(FIELDS) == {"title", "year", "notes"}
    assert "const LX_ALBUM_FIELDS = [['title', 'Title'], ['year', 'Year'], ['notes', 'Notes']]" in APP_JS
    # View Recording: the album skill, chat only.
    assert "skill: isStudioKind ? 'album' : 'recording', subjectType: 'recording'" in APP_JS
    assert "const albumLx = isStudioKind && canEdit && showResolver" in APP_JS
    # The current shape sent for a folder.
    i = APP_JS.index("function collectAlbumMeta()")
    body = APP_JS[i:i + 700]
    for k in ("artist:", "title:", "year:", "number:", "title: t.title", "duration:", "songwriter:",
              "info_file_content:", "notes:", "fingerprint:"):
        assert k in body


def test_queue_uses_album_skill_for_studio_rows():
    i = APP_JS.index("async function _lxQProcess(")
    body = APP_JS[i:i + 1800]
    assert "const skill = it.kind === 'studio' ? 'album' : 'recording'" in body
    assert "API.lomax.runs({ skill, " in body
    assert re.search(r"API\.lomax\.start\(\{\s*skill, ", body)
    assert "skill === 'album' ? lomaxAlbumCurrentFromScan(scan) : lomaxCurrentFromScan(scan)" in body
    j = APP_JS.index("function lomaxAlbumCurrentFromScan(")
    cur = APP_JS[j:j + 700]
    assert "val('album')" in cur and "d.year" in cur and "info_file_content" in cur
    # Queue rows carry kind (the Python that builds them).
    assert "item.kind = resolved.kind" in (ROOT / "app/utils/bulk_ingest_run.py").read_text()


def test_mb_tab_wiring_and_data_paths():
    r = _review()
    assert "API.ingest.releaseCandidates(val('f-artist'), st.title, (ingest.tracks || []).length)" in r
    assert "(res && res.candidates)" in r and "((await API.ingest.releaseDetail(mbid)) || {}).release" in r
    assert "api/ingest/release-candidates?artist=" in API_JS and "api/ingest/release-detail?mbid=" in API_JS
    # Search only on a click; fills only empty fields; no score shown; Unlink wording reused.
    assert "addEventListener('click', search)" in r
    assert "!titleEl.value.trim()" in r and "!yearEl.value.trim()" in r and r.count("/^track\\s*\\d+$/i") == 1
    assert "pp-mb-cand-score" not in r and "Unlink" in r and "Linked by you" in r
    # Saved with the record, studio only; cleared with the folder. Not shown above the
    # tracks any more (Ryan, 2026-10-06): the MusicBrainz tab shows the release facts.
    assert "payload.mb_release_id = (payload.kind === 'studio' && ingest.mb && ingest.mb.picked)" in r
    assert 'id="ingest-mb-facts"' not in r and "ingest.mb = null" in r
    assert "mbReleaseDetailsHtml(st.picked.detail)" in r
    # Facts line is label, catalog number, country, as on View Recording.
    f = APP_JS[APP_JS.index("function ingestMbFactsHtml()"):][:500]
    assert "[p.label, p.catalog_number, p.country]" in f


def test_notes_field_present_and_sent():
    r = _review()
    assert 'id="f-notes"' in r and "<label>Notes</label>" in r
    assert "f.notes           = document.getElementById('f-notes').value.trim()" in APP_JS
    # Sent as `notes` via the form spread, read by the server.
    assert "...f," in r
    assert 'notes                = data.get("notes")' in (ROOT / "app/api/ingest.py").read_text()
    # View Recording shows rec.notes for every kind.
    assert "id=\"rec-notes\"" in APP_JS


def test_rail_is_wide_enough():
    css = (ROOT / "app/static/css/main.css").read_text()
    m = re.search(r"--idx-w:\s*(\d+)px", css)
    assert m and int(m.group(1)) >= 116
    assert re.search(r"\.slide-index \{[^}]*padding: 10px 8px 10px 0;", css, re.S)
