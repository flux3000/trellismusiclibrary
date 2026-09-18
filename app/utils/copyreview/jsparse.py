"""
jsparse.py — stage A of extraction: the JavaScript CST.

Trellis renders its UI by assigning HTML strings into the DOM, so almost all
user-facing copy lives INSIDE template literals rather than standing alone as
string literals. A literal-only extractor returns thousands of class names and
selectors and misses the sentences. This module therefore does one job only:
reconstruct every string-producing expression back into the text it will
produce, while keeping an exact byte range for each run of source that
contributed to it. Stage B (htmlscan.py) finds the copy inside that text.

The byte ranges are the load-bearing part. They are what makes write-back a
splice rather than a regex, and a regex over app.js is how a tool like this
ships a wrong edit.

RAW TEXT, DELIBERATELY. Reconstruction keeps the source's own escape sequences
(`\\'`, `\\n`) rather than resolving them, so an offset into the reconstructed
string maps to a real offset in the file. Unescaping is a display concern and
happens at the edge, in `display_text()`. Resolving escapes here would make
every offset after the first escape wrong by a character, which is exactly the
class of bug that produces a confident, broken edit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

import tree_sitter_javascript as _tsjs
from tree_sitter import Language, Parser, Node

_LANGUAGE = Language(_tsjs.language())

# Sentinel standing in for an interpolation or a non-literal concatenation
# operand. The reviewed unit is the sentence a user reads, so `Load more (5
# left)` is reviewed as `Load more ({} left)` — one decision, not one per
# possible value.
HOLE = "{}"


def parser() -> Parser:
    return Parser(_LANGUAGE)


def parse(source: bytes) -> Node:
    """Parse and return the root node. Raises if the file does not parse."""
    tree = parser().parse(source)
    if tree.root_node.has_error:
        raise SyntaxError("source does not parse cleanly")
    return tree.root_node


@dataclass
class Segment:
    """One run of literal source text contributing to a reconstructed string.

    `recon_*` are character offsets into the reconstruction; `byte_*` are byte
    offsets into the file. Segments are verbatim copies, so a character offset
    inside one converts to a byte offset by encoding the prefix.
    """

    recon_start: int
    recon_end: int
    byte_start: int
    byte_end: int
    text: str

    def byte_at(self, recon_offset: int) -> int:
        """Byte offset in the file for a character offset in the reconstruction."""
        if not (self.recon_start <= recon_offset <= self.recon_end):
            raise ValueError("offset outside segment")
        prefix = self.text[: recon_offset - self.recon_start]
        return self.byte_start + len(prefix.encode("utf-8"))


@dataclass
class Hole:
    """An interpolation or non-literal operand, and the source it stood for.

    The source text is kept because some holes are not really holes: a
    `cond ? 's' : ''` plural has two knowable outcomes, and reviewing the
    sentence with a `{}` in the middle of a word hides which one a user sees.
    """

    recon_offset: int
    source: str


@dataclass
class Literal:
    """A string-producing expression, reconstructed with `{}` for every hole."""

    file: str
    kind: str  # 'string' | 'template' | 'concat'
    text: str  # reconstruction, raw escapes intact
    segments: list[Segment]
    quote: str  # "'", '"' or '`' — the enclosing quote of the FIRST segment
    node_start: int  # byte range of the whole expression, for removal
    node_end: int
    line: int  # 1-based, of the whole expression
    holes: list[Hole] = field(default_factory=list)
    ctx: "Context" = field(default_factory=lambda: Context())

    @property
    def has_holes(self) -> bool:
        return HOLE in self.text

    def spans(self, start: int, end: int) -> list[tuple[int, int]]:
        """Byte ranges in the file covering reconstruction characters [start, end).

        Returns one range per segment touched. More than one means the text
        straddles a hole, so any replacement has to be split the same way.
        """
        out: list[tuple[int, int]] = []
        for seg in self.segments:
            lo = max(start, seg.recon_start)
            hi = min(end, seg.recon_end)
            if lo < hi:
                out.append((seg.byte_at(lo), seg.byte_at(hi)))
        return out

    def quote_at(self, recon_offset: int) -> str:
        """Enclosing quote character for the segment holding this offset.

        A concatenation can mix quote styles, and escaping a replacement for
        the wrong one closes the literal early.
        """
        for seg in self.segments:
            if seg.recon_start <= recon_offset < seg.recon_end:
                return seg.__dict__.get("_quote", self.quote)
        return self.quote


# ── Context gathered from the ancestors of a literal ────────────────────────
# Everything the oracles and the scenario classifier need, collected in one
# upward walk rather than by each of them walking the tree again.


@dataclass
class Context:
    call_name: str | None = None  # e.g. 'querySelector', 'classList.add'
    call_arg_index: int | None = None
    in_catch: bool = False
    compared_with: str | None = None  # source text of the other side of ===
    is_object_key: bool = False
    object_key: str | None = None  # the key this literal is the VALUE of
    guard_text: str | None = None  # condition of the nearest enclosing if
    enclosing_fn: Node | None = None  # innermost function
    owner_fn: Node | None = None  # outermost function below module level
    template_attr: str | None = None  # filled in by htmlscan for attributes


_COMPARISONS = {"===", "!==", "==", "!="}
_FN_TYPES = {
    "function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
    "generator_function",
    "generator_function_declaration",
}


def callee_name(call: Node, source: bytes) -> str:
    """'querySelector' or 'classList.add' — the tail of the callee, not the object.

    Keeping the last two identifiers is what lets the selector oracle match
    `el.classList.add` and `document.querySelector` with one rule.
    """
    fn = call.child_by_field_name("function")
    if fn is None:
        return ""
    if fn.type == "identifier":
        return text_of(fn, source)
    if fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        obj = fn.child_by_field_name("object")
        name = text_of(prop, source) if prop else ""
        if obj is not None and obj.type == "member_expression":
            inner = obj.child_by_field_name("property")
            if inner is not None:
                return f"{text_of(inner, source)}.{name}"
        if obj is not None and obj.type == "identifier":
            return f"{text_of(obj, source)}.{name}"
        return name
    return ""


def text_of(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def build_context(node: Node, source: bytes) -> Context:
    """Walk up from a literal, collecting every signal in one pass."""
    ctx = Context()
    child = node
    parent = node.parent
    fn_chain: list[Node] = []

    while parent is not None:
        t = parent.type

        if t in _FN_TYPES:
            fn_chain.append(parent)

        elif t == "catch_clause":
            ctx.in_catch = True

        elif t == "arguments" and ctx.call_name is None:
            call = parent.parent
            if call is not None and call.type in ("call_expression", "new_expression"):
                ctx.call_name = callee_name(call, source)
                named = [c for c in parent.children if c.is_named]
                for i, arg in enumerate(named):
                    if arg.start_byte <= child.start_byte and child.end_byte <= arg.end_byte:
                        ctx.call_arg_index = i
                        break

        elif t == "binary_expression":
            op = parent.child_by_field_name("operator")
            op_text = text_of(op, source) if op is not None else ""
            if op_text in _COMPARISONS:
                left = parent.child_by_field_name("left")
                right = parent.child_by_field_name("right")
                other = right if left is child else left
                if other is not None:
                    ctx.compared_with = text_of(other, source)

        elif t == "pair":
            key = parent.child_by_field_name("key")
            value = parent.child_by_field_name("value")
            if key is child:
                ctx.is_object_key = True
            elif value is child and key is not None and ctx.object_key is None:
                ctx.object_key = text_of(key, source).strip("'\"")

        elif t in ("if_statement", "ternary_expression") and ctx.guard_text is None:
            cond = parent.child_by_field_name("condition")
            if cond is not None and cond.end_byte <= child.start_byte:
                ctx.guard_text = text_of(cond, source)

        child = parent
        parent = parent.parent

    if fn_chain:
        ctx.enclosing_fn = fn_chain[0]
        # The outermost function is app.js's module IIFE; the one below it is
        # the unit of ownership. `owner()` has to accept arrow and named
        # function expressions or 63 strings land in "(module)".
        ctx.owner_fn = fn_chain[-2] if len(fn_chain) > 1 else fn_chain[-1]
    return ctx


# ── Reconstruction ──────────────────────────────────────────────────────────


def _string_segments(
    node: Node, source: bytes, recon_base: int
) -> tuple[str, list[Segment], list[Hole]]:
    """Reconstruct a `string` node: its content, minus the quotes."""
    parts: list[Segment] = []
    holes: list[Hole] = []
    buf: list[str] = []
    cursor = recon_base
    for child in node.children:
        if child.type in ("'", '"', "`"):
            continue
        chunk = text_of(child, source)
        if child.type == "template_substitution":
            holes.append(Hole(cursor, chunk))
            buf.append(HOLE)
            cursor += len(HOLE)
            continue
        seg = Segment(cursor, cursor + len(chunk), child.start_byte, child.end_byte, chunk)
        parts.append(seg)
        buf.append(chunk)
        cursor += len(chunk)
    return "".join(buf), parts, holes


def _quote_of(node: Node, source: bytes) -> str:
    first = source[node.start_byte : node.start_byte + 1].decode("utf-8", "replace")
    return first if first in ("'", '"', "`") else "'"


def _flatten_concat(node: Node) -> list[Node]:
    """Left-associative `a + b + c` flattened to its operands, in order."""
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "binary_expression":
            op = n.child_by_field_name("operator")
            if op is not None and op.type == "+":
                left = n.child_by_field_name("left")
                right = n.child_by_field_name("right")
                if left is not None:
                    walk(left)
                if right is not None:
                    walk(right)
                return
        out.append(n)

    walk(node)
    return out


def _is_string_node(node: Node) -> bool:
    return node.type in ("string", "template_string")


def reconstruct(node: Node, source: bytes, file: str) -> Literal | None:
    """Turn one string-producing expression into a Literal, or None."""
    if node.type in ("string", "template_string"):
        text, segs, holes = _string_segments(node, source, 0)
        quote = _quote_of(node, source)
        for s in segs:
            s.__dict__["_quote"] = quote
        return Literal(
            file=file,
            kind="template" if node.type == "template_string" else "string",
            text=text,
            segments=segs,
            quote=quote,
            node_start=node.start_byte,
            node_end=node.end_byte,
            line=node.start_point[0] + 1,
            holes=holes,
            ctx=build_context(node, source),
        )

    if node.type == "binary_expression":
        op = node.child_by_field_name("operator")
        if op is None or op.type != "+":
            return None
        operands = _flatten_concat(node)
        if not any(_is_string_node(o) for o in operands):
            return None
        buf: list[str] = []
        segs: list[Segment] = []
        holes: list[Hole] = []
        cursor = 0
        for operand in operands:
            if _is_string_node(operand):
                sub_text, sub_segs, sub_holes = _string_segments(operand, source, cursor)
                q = _quote_of(operand, source)
                for s in sub_segs:
                    s.__dict__["_quote"] = q
                buf.append(sub_text)
                segs.extend(sub_segs)
                holes.extend(sub_holes)
                cursor += len(sub_text)
            else:
                holes.append(Hole(cursor, text_of(operand, source)))
                buf.append(HOLE)
                cursor += len(HOLE)
        first_quote = segs[0].__dict__.get("_quote", "'") if segs else "'"
        return Literal(
            file=file,
            kind="concat",
            text="".join(buf),
            segments=segs,
            quote=first_quote,
            node_start=node.start_byte,
            node_end=node.end_byte,
            line=node.start_point[0] + 1,
            holes=holes,
            ctx=build_context(node, source),
        )

    return None


def literals(root: Node, source: bytes, file: str) -> Iterator[Literal]:
    """Every string-producing expression in the file, outermost first.

    A concatenation is yielded whole and its string operands are NOT yielded
    again — `'Failed: ' + e.message` is one sentence to review, not a fragment
    plus a mystery.
    """
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "binary_expression":
            op = node.child_by_field_name("operator")
            if op is not None and op.type == "+":
                lit = reconstruct(node, source, file)
                if lit is not None:
                    yield lit
                    # Descend only into the non-literal operands, so an
                    # interpolated template inside a concatenation still gets
                    # its own holes explored, but the string parts are not
                    # re-reported.
                    for operand in _flatten_concat(node):
                        if not _is_string_node(operand):
                            stack.append(operand)
                    continue
        if node.type in ("string", "template_string"):
            lit = reconstruct(node, source, file)
            if lit is not None:
                yield lit
            # Interpolations can contain further templates; those are real
            # copy too (a nested `${cards.map(c => `<div>…`)}`).
            for child in node.children:
                if child.type == "template_substitution":
                    stack.append(child)
            continue
        stack.extend(node.children)


# ── Display ─────────────────────────────────────────────────────────────────

_ESCAPES = {
    "\\n": "\n",
    "\\t": "\t",
    "\\r": "\r",
    "\\'": "'",
    '\\"': '"',
    "\\`": "`",
    "\\\\": "\\",
    "\\$": "$",
}
_ESCAPE_RE = re.compile(r"\\u\{[0-9a-fA-F]+\}|\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}|\\[ntr'\"`\\$]")


def _unescape(match: re.Match) -> str:
    token = match.group(0)
    if token in _ESCAPES:
        return _ESCAPES[token]
    # \u2026 is an ellipsis to a reader and six characters to a regex. Copy is
    # reviewed as the reader sees it, so these resolve here too.
    digits = token[2:].strip("{}")
    try:
        return chr(int(digits, 16))
    except ValueError:
        return token


def display_text(raw: str) -> str:
    """Resolve source escapes for showing a string to a human."""
    return _ESCAPE_RE.sub(_unescape, raw)


_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Identity form: escapes resolved, whitespace runs collapsed, trimmed.

    Indentation inside a template literal is layout, not copy, so it must not
    change a string's identity — otherwise reindenting a block puts every
    approved string back in the queue.
    """
    return _WS.sub(" ", display_text(text)).strip()
