"""
Every underscore-prefixed helper CALLED in app.js must be DEFINED in app.js.

2026-09-27: a rebuild retired _biMetaLine but left one call behind. node
--check and test_no_undefined_names both passed; the first user to open the
page got "Can't find variable: _biMetaLine" and a stalled ingest. Private
helpers in app.js all start with an underscore, which makes this check cheap
and precise: a call to _foo( with no `function _foo(` / `const _foo =` /
`let _foo` anywhere in the file is a bug, not a style choice.
"""
import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "app.js"

_CALL = re.compile(r"(?<![\w.$])(_[A-Za-z][A-Za-z0-9_]*)\s*\(")
_DEF  = re.compile(
    r"(?:function\s+(_[A-Za-z][A-Za-z0-9_]*)\s*\()"
    r"|(?:(?:const|let|var)\s+(_[A-Za-z][A-Za-z0-9_]*)\s*=)"
    r"|(?:^\s*(_[A-Za-z][A-Za-z0-9_]*)\s*[:(]\s*(?:function|\(|[A-Za-z_]))",
    re.M,
)


def test_every_called_private_helper_is_defined():
    src = APP_JS.read_text(encoding="utf-8")
    # Strip line comments so a dated note like "_promptCreate() removed" does
    # not read as a call. Block comments are rare in app.js; a call inside one
    # would be a false positive, not a missed bug.
    code = re.sub(r"//[^\n]*", "", src)
    called = set(_CALL.findall(code))
    defined = {n for m in _DEF.finditer(src) for n in m.groups() if n}
    # Object-method calls (obj._x()) and property keys are excluded by the
    # lookbehind and by only counting bare calls; anything left is a real
    # free-standing call.
    missing = sorted(called - defined)
    assert not missing, f"called but never defined in app.js: {missing}"
