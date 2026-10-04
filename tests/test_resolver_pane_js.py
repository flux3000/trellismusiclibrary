"""The Resolver pane builder (app.js buildResolverPaneHtml) lists every field with its
evidence for a Resolver v2 recording and degrades to the conflict-only view for an older
one; the review labels know tentative:<field>. Runs tests/js_resolver_pane.js."""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(__file__)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_resolver_pane_with_and_without_evidence():
    app_js = os.path.join(HERE, "..", "app", "static", "js", "app.js")
    r = subprocess.run(["node", os.path.join(HERE, "js_resolver_pane.js"), app_js],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
