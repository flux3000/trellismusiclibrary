"""The Import page removes each ingested row as its own ingest finishes: runs
tests/js_bi_incremental_rows.js (extract-and-run of the pure helpers in app.js)."""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(__file__)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_incremental_row_helpers():
    app_js = os.path.join(HERE, "..", "app", "static", "js", "app.js")
    r = subprocess.run(["node", os.path.join(HERE, "js_bi_incremental_rows.js"), app_js],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
