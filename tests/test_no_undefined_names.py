"""
tests/test_no_undefined_names.py — catch NameErrors before they reach a request.

WHY

`py_compile` checks SYNTAX. It does not check that the names a function uses
exist, because that is a runtime question. So this compiles cleanly and then
500s on the first request:

    visible = ...            # never written — a patch aborted halfway
    ...
    if r.id in visible       # NameError, at request time

That happened twice in one session on 2026-08-24: once a patch script aborted
mid-run leaving a function referencing an undefined local, and once
`peer_visible_venue_ids` / `peer_visible_artist_ids` were used in api/share.py
without ever being imported. Both compiled. Both were NameErrors on the first
peer request. This is the Python counterpart of "node --check is not enough".

SCOPE AWARENESS IS THE WHOLE DIFFICULTY

A naive walker reports ~16 false positives on this codebase, because Python
names come from more places than a flat scan sees:

  * CLOSURES — a nested `def prog()` reading `job` from its enclosing function
  * conditional module-level bindings — `_HAS_KEYRING` assigned inside
    `try: import keyring / except ImportError:`, which is not at col_offset 0
  * comprehension targets, `except X as e`, walrus, global/nonlocal

So this walks real scopes: module → function → nested function, seeding each
with everything visible from outside it, and never descending into a nested
scope while checking the outer one. A test that cries wolf is worse than no
test — it gets muted, and then it is not there when it matters.

DELIBERATELY FLOW-INSENSITIVE. It hunts names bound NOWHERE in any enclosing
scope, not names possibly-used-before-assignment. That is the bug class above,
and it keeps the check free of false positives.
"""

import ast
import builtins
import io
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
TARGETS = sorted(
    list((REPO / "app" / "api").glob("*.py"))
    + list((REPO / "app" / "utils").glob("*.py"))
    + list((REPO / "app" / "models").glob("*.py"))
)

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _own_bindings(scope):
    """Every name bound directly in `scope`, NOT descending into nested scopes.

    Descent stops at nested functions/lambdas/classes — their bodies are their
    own scope — but continues through `try`, `if`, `with`, `for` and every
    other statement, which is what a col_offset check gets wrong.
    """
    out = set()

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (*_SCOPES, ast.ClassDef)):
                if not isinstance(child, ast.Lambda):
                    out.add(child.name)
                continue                       # its body is a different scope
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for a in child.names:
                    out.add((a.asname or a.name).split(".")[0])
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                out.add(child.id)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                out.add(child.name)
            elif isinstance(child, (ast.Global, ast.Nonlocal)):
                out.update(child.names)
            visit(child)

    visit(scope)
    return out


def _params(fn):
    a = fn.args
    names = {p.arg for p in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)}
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


def _loads_and_nested(scope):
    """Name loads in this scope only, plus the nested scopes to recurse into."""
    loads, nested = [], []

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPES):
                nested.append(child)
                continue
            if isinstance(child, ast.ClassDef):
                # A method cannot see class scope, so its body is checked
                # against the MODULE scope — collected here, recursed below.
                for sub in ast.iter_child_nodes(child):
                    if isinstance(sub, _SCOPES):
                        nested.append(sub)
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                loads.append(child)
            visit(child)

    visit(scope)
    return loads, nested


def _check_scope(scope, enclosing, path, problems):
    bound = set(enclosing) | _own_bindings(scope)
    if isinstance(scope, _SCOPES):
        bound |= _params(scope)

    loads, nested = _loads_and_nested(scope)
    label = getattr(scope, "name", "<lambda>")
    for n in loads:
        if n.id not in bound:
            problems.append(f"{path.name}:{n.lineno} in {label}(): {n.id}")

    for child in nested:
        _check_scope(child, bound, path, problems)


@pytest.mark.parametrize("path", TARGETS, ids=lambda p: p.name)
def test_module_has_no_undefined_names(path):
    tree = ast.parse(io.open(path, encoding="utf-8").read())

    module_scope = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    module_scope |= _own_bindings(tree)

    problems = []
    _, nested = _loads_and_nested(tree)
    for fn in nested:
        _check_scope(fn, module_scope, path, problems)

    assert not problems, (
        "Names used but never bound in any enclosing scope — NameErrors "
        "waiting for a request:\n  " + "\n  ".join(sorted(set(problems))))
