"""
Add Recording opened on an Album row (a studio record): the form reads scan.resolved.kind and scan.resolved.album.value,
draws the album layout, and sends kind and title on save. JS is checked by source, the server by a
real confirm.
"""
import re
from pathlib import Path

from app.extensions import db as _db
from app.models.recording import Recording
from app.utils import bulk_ingest_run as bir

from tests.test_learned_aliases import env, _flac  # noqa: F401  (fixture and helper)

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")


def _fn(name):
    i = APP_JS.index(f"function {name}(")
    return APP_JS[i:i + 60000]


def _review():
    i = APP_JS.index("function renderIngestReview()")
    j = APP_JS.index("function fmtDur(", i)
    return APP_JS[i:j]


def test_form_reads_scan_kind_and_title():
    r = _review()
    assert "ingest.scan.resolved?.kind === 'studio'" in r
    assert "ingest.kindFolder !== ingest.folderPath" in r
    assert "f.album_title     = ingest.scan.resolved?.album?.value || ''" in r
    assert "ingest.kindFolder = null" in APP_JS            # reset with the wizard state


def test_confirm_body_carries_kind_and_title():
    r = _review()
    assert "kind: ingest.kind === 'studio' ? 'studio' : 'live'," in r
    assert "payload.title = f.album_title || null" in r
    assert "delete payload.album_title" in r


def test_studio_layout_hides_live_only_fields():
    r = _review()
    assert "Album Title" in r and 'id="f-album-title"' in r
    assert APP_JS.count("Album Title") == 1
    # Month, Day, Venue, Event, Stage, end date, location, quality/source/lineage, source tag/shnid.
    assert r.count("${hid}") == 5
    assert r.count("${studio ? '; display:none' : ''}") == 4
    # Hidden live fields are blanked on an album save.
    for k in ("venue_name: ''", "city: ''", "source_tag: ''", "etree_shnid: ''", "start_month: null"):
        assert k in r


def test_studio_drops_resolver_and_info_tabs():
    r = _review()
    assert "resolver: hasResolver && !studio, research: true" in r and "info: !studio" in r
    assert "_hasTab('isp-resolver')" in r and "'isp-info'" in r


def test_classify_button_uses_existing_labels_and_flips_kind():
    r = _review()
    assert "studio ? 'Classify as Live Recording' : 'Classify as Album'" in r
    m = re.search(r"btn-classify-kind'\)\?\.addEventListener\('click', \(\) => \{(.*?)\n    \}\)", r, re.S)
    assert m and "_syncIngestFormFromDom()" in m.group(1) and "renderIngestStep()" in m.group(1)
    assert "ingest.kind === 'studio' ? 'live' : 'studio'" in m.group(1)


def test_album_completeness_counts_artist_year_tracks_only():
    f = _fn("_studioCompleteness")[:900]
    assert "'artist'" in f and "'date'" in f and "'tracks'" in f
    assert "venue" not in f and "city" not in f
    assert "if (ingest.kind === 'studio')" in _fn("reScore")[:900]


def test_album_save_is_not_blocked_by_date_or_venue():
    sub = APP_JS[APP_JS.index("const _submitReview"):][:1500]
    assert "Artist name is required." in sub
    assert not re.search(r"(year|venue|date)[^\n]*is required", sub, re.I)


def test_studio_confirm_saves_kind_and_title(env):
    from app.api.ingest import _do_confirm
    _flac(env.lib / "Album" / "01.flac", ARTIST="Some Band", TITLE="One")
    payload = {
        "source_folder_path": str(env.lib / "Album"), "artist_name": "Some Band",
        "start_year": 1994, "start_month": None, "start_day": None, "venue_name": "",
        "kind": "studio", "title": "Second Record", "is_complete": True, "skip_analysis": True,
        "tracks": [{"track_number": 1, "title": "One", "filename": "01.flac"}],
    }
    out = _do_confirm(payload, bir._owner_user_id())
    rec = _db.session.get(Recording, out["recording_id"])
    assert rec.kind == "studio" and rec.title == "Second Record"


def test_scan_payload_carries_kind_and_album_on_resolved():
    """The form reads these two keys; the scan endpoint serialises Resolved.to_dict() as resp["resolved"].
    A top-level scan.kind does not exist (that is what broke the first build of the album form)."""
    src = (ROOT / "app/utils/resolve.py").read_text()
    body = src[src.index("def to_dict"):]
    assert '"kind":' in body and '"album":' in body
