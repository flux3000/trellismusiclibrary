"""The Lomax Resolver table, the re-applied accepted decisions, the Ask button, the billing comparison
and the queue's Metadata pill, run headlessly from the real app.js. Runs tests/js_lomax_resolver.js."""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(__file__)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_lomax_resolver_helpers_in_app_js():
    app_js = os.path.join(HERE, "..", "app", "static", "js", "app.js")
    r = subprocess.run(["node", os.path.join(HERE, "js_lomax_resolver.js"), app_js],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
