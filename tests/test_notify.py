"""Errors go through notify.js, never the system alert()."""
import re
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js"
CALL = re.compile(r"(?<![\w.])alert\(")


def test_no_system_alert_in_app_scripts():
    for name in ("app.js", "api.js", "player.js", "debug.js", "main.js"):
        for n, line in enumerate((JS / name).read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("//"):
                continue
            assert not CALL.search(line), f"{name}:{n} uses alert(); use Notify"


def test_notify_loads_before_its_callers():
    html = (JS.parent / "index.html").read_text(encoding="utf-8")
    order = [html.index(f'/js/{s}.js') for s in ("notify", "api", "player", "app")]
    assert order == sorted(order)
