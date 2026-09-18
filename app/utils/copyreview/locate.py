"""
locate.py — where a string lives, derived from the code.

Three answers, none of them stored in a table this tool maintains:

* **Section** comes from the banner comments already in app.js
  (`// ── Ingest wizard ──`). There are around a hundred of them, they cover
  the whole file, and they are maintained as part of the code — which is the
  entire argument for using them. A section map inside the tool would be
  correct on the day it was written.

* **Function** comes from the CST.

* **Page** comes from `route()`, which is one if/else chain over the URL hash.
  A call graph from each branch labels a function with the routes that reach
  it. It labels; it does not partition. Most of app.js is reachable from most
  pages, so anything reachable from four or more routes is shared chrome and
  saying so is more honest than picking one.

⚠ The trap, found while planning this: banner comments exist at two
indentation levels. "Nearest preceding banner" therefore put 156 unrelated
strings into one section, because a banner inside one function captured
everything after it. Banners are scoped by the enclosing function's byte range
instead — the section of a string is the banner preceding the FUNCTION that
owns it, not the banner preceding the string.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass

from tree_sitter import Node

from .jsparse import text_of

SHARED = "Shared / global chrome"

# How far to follow the call graph out of a route's branch. Unbounded, every
# render function reaches every other one through the shared helpers
# (`setMainHTML`, `renderSidebar`, `esc`), the minimum route count per function
# comes out at four, and every single string ends up labelled "shared" — which
# is true and useless. Two hops keeps the label attached to the view that
# actually owns the copy.
MAX_HOPS = 2
MODULE = "(module)"

_BANNER = re.compile(r"^(\s*)//\s*[─—-]{2,}\s*(.*?)\s*[─—-]*\s*$")


@dataclass
class Banner:
    byte: int
    label: str
    indent: int


def banners(source: bytes) -> list[Banner]:
    """Banner comments, in file order, with the column they start at."""
    out: list[Banner] = []
    offset = 0
    for line in source.decode("utf-8", "replace").splitlines(keepends=True):
        match = _BANNER.match(line.rstrip("\n"))
        if match and match.group(2):
            out.append(Banner(byte=offset, label=match.group(2).strip(), indent=len(match.group(1))))
        offset += len(line.encode("utf-8"))
    return out


def section_at(byte: int, banner_list: list[Banner]) -> str:
    """Nearest banner at or before `byte`."""
    best = MODULE
    for banner in banner_list:
        if banner.byte <= byte:
            best = banner.label
        else:
            break
    return best


# ── Functions ───────────────────────────────────────────────────────────────

_FN_TYPES = {
    "function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
    "generator_function",
    "generator_function_declaration",
}


def function_name(node: Node, source: bytes) -> str:
    """A usable name for any function form, including the expression forms.

    Arrow functions and named function expressions are assigned to variables
    and properties, so the name lives on the parent. Missing this puts 63
    strings in "(module)".
    """
    named = node.child_by_field_name("name")
    if named is not None:
        return text_of(named, source)

    parent = node.parent
    if parent is None:
        return "(anonymous)"
    if parent.type == "variable_declarator":
        name = parent.child_by_field_name("name")
        if name is not None:
            return text_of(name, source)
    if parent.type == "pair":
        key = parent.child_by_field_name("key")
        if key is not None:
            return text_of(key, source).strip("'\"")
    if parent.type == "assignment_expression":
        left = parent.child_by_field_name("left")
        if left is not None:
            return text_of(left, source)
    if parent.type == "method_definition":
        prop = parent.child_by_field_name("name")
        if prop is not None:
            return text_of(prop, source)
    return "(anonymous)"


@dataclass
class Function:
    name: str
    start: int
    end: int
    depth: int
    calls: set[str]


def functions(root: Node, source: bytes) -> list[Function]:
    """Every function in the file with its byte range, depth and callees."""
    out: list[Function] = []

    def walk(node: Node, depth: int) -> None:
        next_depth = depth
        if node.type in _FN_TYPES:
            out.append(
                Function(
                    name=function_name(node, source),
                    start=node.start_byte,
                    end=node.end_byte,
                    depth=depth,
                    calls=set(),
                )
            )
            next_depth = depth + 1
        for child in node.children:
            walk(child, next_depth)

    walk(root, 0)
    # Callees are attributed to the innermost enclosing function that HAS a
    # name. The graph is keyed by name, so attributing to an anonymous arrow
    # collapses every event handler into one node called "(anonymous)" and
    # every call chain that runs through a click handler goes dark — which is
    # most of them, in an app wired up this way.
    by_range = sorted(out, key=lambda f: (f.start, -f.end))
    for call_name, at in _calls(root, source):
        enclosing = [f for f in by_range if f.start <= at < f.end]
        named = [f for f in enclosing if f.name != "(anonymous)"]
        target = named[-1] if named else (enclosing[-1] if enclosing else None)
        if target is not None:
            target.calls.add(call_name)
    return out


def _calls(root: Node, source: bytes) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                found.append((text_of(fn, source), node.start_byte))
        stack.extend(node.children)
    return found


def owner_of(byte: int, fns: list[Function]) -> Function | None:
    """The outermost function below module level that contains `byte`.

    app.js is one module-level IIFE, so depth 0 is the whole file and depth 1
    is the unit people think of as "a function".
    """
    candidates = [f for f in fns if f.start <= byte < f.end]
    if not candidates:
        return None
    lowest = min(f.depth for f in candidates)
    target_depth = lowest if lowest > 0 else min((f.depth for f in candidates if f.depth > 0), default=lowest)
    at_depth = [f for f in candidates if f.depth == target_depth]
    return min(at_depth, key=lambda f: f.end - f.start) if at_depth else candidates[0]


# ── Pages, from route() ─────────────────────────────────────────────────────

_HASH_LITERAL = re.compile(r"'(#[^']*)'|\"(#[^\"]*)\"")


def route_entries(root: Node, source: bytes) -> dict[str, set[str]]:
    """Route hash → the functions its branch calls directly."""
    route_fn = None
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _FN_TYPES and function_name(node, source) == "route":
            route_fn = node
            break
        stack.extend(node.children)
    if route_fn is None:
        return {}

    entries: dict[str, set[str]] = {}
    body = route_fn.child_by_field_name("body")
    if body is None:
        return {}

    def branches(node: Node) -> None:
        if node.type == "if_statement":
            cond = node.child_by_field_name("condition")
            conseq = node.child_by_field_name("consequence")
            alt = node.child_by_field_name("alternative")
            if cond is not None and conseq is not None:
                hashes = [
                    (m.group(1) or m.group(2)) for m in _HASH_LITERAL.finditer(text_of(cond, source))
                ]
                called = {name for name, _ in _calls(conseq, source)}
                for h in hashes or ["(unmatched branch)"]:
                    entries.setdefault(h, set()).update(called)
            if alt is not None:
                branches(alt)
            return
        for child in node.children:
            branches(child)

    branches(body)
    return entries


def pages_by_function(root: Node, source: bytes, fns: list[Function]) -> dict[str, set[str]]:
    """Function name → the set of route hashes that can reach it."""
    graph: dict[str, set[str]] = {}
    for fn in fns:
        graph.setdefault(fn.name, set()).update(fn.calls)

    reach: dict[str, set[str]] = {}
    for route, seeds in route_entries(root, source).items():
        seen: set[str] = set()
        queue = deque((name, 0) for name in seeds)
        while queue:
            name, depth = queue.popleft()
            if name in seen:
                continue
            seen.add(name)
            if depth < MAX_HOPS:
                queue.extend((callee, depth + 1) for callee in graph.get(name, ()))
        for name in seen:
            reach.setdefault(name, set()).add(route)
    return reach


def page_label(routes: set[str]) -> str:
    """One label from a set of reaching routes."""
    if not routes:
        return "Unrouted"
    if len(routes) >= 4:
        return SHARED
    return ", ".join(sorted(routes))
