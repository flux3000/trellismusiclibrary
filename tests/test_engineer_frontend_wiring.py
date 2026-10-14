"""Static checks that the Engineer UI is wired end to end in app.js (no browser needed)."""
import re
from pathlib import Path

JS = (Path(__file__).resolve().parent.parent / "app/static/js/app.js").read_text()
FIELDS = ("recording_engineer", "transfer_engineer", "mastering_engineer")


def test_lomax_fields_include_the_three_engineer_credits():
    block = re.search(r"const LX_FIELDS = \[(.*?)\n  \]", JS, re.S).group(1)
    for f in FIELDS:
        assert "'%s'" % f in block


def test_add_recording_form_reads_writes_and_sends_the_credits():
    for dom in ("f-rec-eng", "f-xfer-eng", "f-mast-eng"):
        assert "id=\"${id}\"" in JS and "'%s'" % dom in JS
    for f in FIELDS:
        assert "getFormField" in JS and "case '%s'" % f in JS          # read and write mappings
        assert "rv('%s')" % f in JS                                    # prefill from the resolver
        assert "'%s'" % f in re.search(r"_INGEST_SNAPSHOT_KEYS = \[(.*?)\]", JS, re.S).group(1)


def test_engineer_routes_nav_and_search_exist():
    assert "hash === '#/engineers'" in JS and "hash.startsWith('#/engineer/')" in JS
    assert "['engineers', 'Engineers']" in JS
    assert "item.type === 'engineer'" in JS
