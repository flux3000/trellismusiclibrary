"""Source-level, because the behaviours live in app.js / run.py / main.css, which
the Python suite cannot run.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "app/static/js/app.js").read_text()
CSS = (ROOT / "app/static/css/main.css").read_text()


def test_webview_store_persists_across_launches():
    """PyWebView's default is a private session whose localStorage is discarded on
    quit; the palette lives in localStorage, so it reset to the default every launch."""
    tree = ast.parse((ROOT / "run.py").read_text())
    starts = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "start"
              and getattr(getattr(n.func, "value", None), "id", "") == "webview"]
    assert starts, "webview.start call not found"
    kw = {k.arg: k.value for k in starts[0].keywords}
    assert isinstance(kw.get("private_mode"), ast.Constant) and kw["private_mode"].value is False
    assert "storage_path" in kw


def test_performance_edits_trigger_the_auto_tag_write():
    """Artist/date/venue/event/stage/personnel edits log no recording event, so only
    an explicit call after the save can honour 'write changes immediately'."""
    assert "async function autoWriteNow()" in APP_JS
    assert len(re.findall(r"await autoWriteNow\(\)", APP_JS)) >= 7


def test_tags_pane_has_no_diff_under_auto_write():
    m = re.search(r"async function loadFileTags\(recId\) \{.*?\n    \}\n", APP_JS, re.S)
    assert m and "autoWrite ? '' : fileTagsDiffHtml" in m.group(0)


def test_browse_page_has_no_collections_module():
    assert "lib-mod-collections" not in APP_JS
    assert "<h1>Live Recordings</h1>" in APP_JS


def test_search_zero_state_copy():
    assert "Search covers artists" not in APP_JS
    assert "Browse Live Recordings</button>" in APP_JS


def test_hero_image_position_and_margin():
    assert re.search(r"\.b-img img \{[^}]*object-position: center 40%", CSS)
    assert re.search(r"\.pp-hero\.b-hero \{ position: relative; margin: -22px -28px 0;", CSS)


def test_lomax_link_proposal_lands_in_the_source_input():
    assert "resSuggest" in APP_JS and "lxCardHtml('Trusted sources'" in APP_JS
