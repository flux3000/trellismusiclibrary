"""
pyapi.py — the API layer's half of the copy.

Roughly two hundred sentences a user can read arrive from Flask rather than
from the frontend, and they are the least reviewed text in the product because
seeing one means making a request fail. They are found by walking the AST of
`app/api/*.py` for the two shapes that actually reach a user: a dict carrying
an `error` or `message` key, and `jsonify(error=…)`.

Two version traps, both avoided by reading the raw source rather than trusting
node positions:

* **f-string children.** Their positions were only made reliable in Python
  3.12. The same code has to give the same byte spans on a 3.10 in a sandbox
  and the 3.13 the app runs on, because a wrong span is a wrong edit.

* **Implicit concatenation.** `("Artist has " f"{n} shows — " "delete them")`
  is ONE node, and its source span covers the quotes, the newlines and the `f`
  prefixes between the parts. Taking the first quote to the last quote reads
  all that punctuation as copy, which is what the long multi-line API errors
  are made of. So the literal is scanned into its parts, and only the content
  of each part is copy.

Values with no literal at all — `str(e)`, a variable, a function call — are
recorded as `runtime` and carry no text. They are listed rather than dropped,
because a silently missing error message is indistinguishable from one that
was reviewed.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from .jsparse import HOLE, Segment

KEYS = ("error", "message")

_PREFIX_CHARS = "rRbBuUfF"


@dataclass
class PyString:
    file: str
    text: str  # placeholder form; '' when the value is computed
    segments: list[Segment]
    quote: str
    line: int
    key: str  # 'error' or 'message'
    form: str  # 'literal' | 'fstring' | 'concat' | 'runtime'
    node_start: int
    node_end: int
    holes: int = 0


@dataclass
class Part:
    """One string literal in a possibly-concatenated expression."""

    content_start: int  # offsets into the raw source segment
    content_end: int
    quote: str
    is_f: bool


def _line_offsets(source: bytes) -> list[int]:
    offsets = [0]
    for i, byte in enumerate(source):
        if byte == 0x0A:
            offsets.append(i + 1)
    return offsets


def _byte_at(line_offsets: list[int], lineno: int, col: int) -> int:
    """ast positions to a byte offset. `col_offset` is already a UTF-8 offset."""
    return line_offsets[lineno - 1] + col


def string_parts(raw: str) -> list[Part]:
    """Scan a string expression's source into its literal parts.

    Deliberately hand-written rather than using `tokenize`, which reports
    f-strings as one token before Python 3.12 and as three after. This behaves
    the same on every version the project runs on.
    """
    parts: list[Part] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch in " \t\r\n\\()+,":
            i += 1
            continue
        if ch == "#":  # a comment between concatenated parts
            while i < n and raw[i] != "\n":
                i += 1
            continue
        prefix_start = i
        while i < n and raw[i] in _PREFIX_CHARS:
            i += 1
        if i >= n or raw[i] not in "\"'":
            # Not a literal after all (a name, an operator). Give up on the
            # rest: anything past here is not something to splice into.
            if i == prefix_start:
                i += 1
            break
        prefix = raw[prefix_start:i]
        is_f = "f" in prefix.lower()
        is_raw = "r" in prefix.lower()
        quote = raw[i : i + 3] if raw[i : i + 3] in ('"""', "'''") else raw[i]
        i += len(quote)
        content_start = i
        while i < n:
            if raw[i] == "\\" and not is_raw:
                i += 2
                continue
            if raw.startswith(quote, i):
                break
            i += 1
        parts.append(Part(content_start, min(i, n), quote, is_f))
        i += len(quote)
    return parts


def fstring_pieces(content: str) -> list[tuple[int, int]]:
    """Literal runs inside an f-string's content, as (start, end) offsets.

    Brace-aware, so `{x!r:>{w}}` is one hole and `{{` is a literal brace
    rather than the start of one.
    """
    pieces: list[tuple[int, int]] = []
    start = 0
    i = 0
    depth = 0
    while i < len(content):
        ch = content[i]
        if depth == 0:
            if content.startswith("{{", i) or content.startswith("}}", i):
                i += 2
                continue
            if ch == "{":
                if i > start:
                    pieces.append((start, i))
                depth = 1
                i += 1
                continue
        else:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    start = i + 1
            i += 1
            continue
        i += 1
    if start < len(content):
        pieces.append((start, len(content)))
    return pieces


def _span(node: ast.AST, offsets: list[int]) -> tuple[int, int]:
    start = _byte_at(offsets, node.lineno, node.col_offset)  # type: ignore[attr-defined]
    end = _byte_at(
        offsets,
        node.end_lineno or node.lineno,  # type: ignore[attr-defined]
        node.end_col_offset or node.col_offset,  # type: ignore[attr-defined]
    )
    return start, end


def _from_literal(
    node: ast.AST, source: bytes, offsets: list[int], file: str, key: str
) -> PyString | None:
    """Build a PyString from a Constant or JoinedStr, parts and holes and all."""
    start, end = _span(node, offsets)
    raw = source[start:end].decode("utf-8", "replace")
    parts = string_parts(raw)
    if not parts:
        return None

    segments: list[Segment] = []
    text: list[str] = []
    cursor = 0
    holes = 0

    def byte_of(offset_in_raw: int) -> int:
        return start + len(raw[:offset_in_raw].encode("utf-8"))

    for part in parts:
        content = raw[part.content_start : part.content_end]
        runs = (
            fstring_pieces(content)
            if part.is_f
            else ([(0, len(content))] if content else [])
        )
        previous_end = 0
        for run_start, run_end in runs:
            if run_start > previous_end or (part.is_f and run_start > 0 and previous_end == 0):
                text.append(HOLE)
                cursor += len(HOLE)
                holes += 1
            chunk = content[run_start:run_end]
            segments.append(
                Segment(
                    cursor,
                    cursor + len(chunk),
                    byte_of(part.content_start + run_start),
                    byte_of(part.content_start + run_end),
                    chunk,
                )
            )
            segments[-1].__dict__["_quote"] = part.quote
            text.append(chunk)
            cursor += len(chunk)
            previous_end = run_end
        if part.is_f and previous_end < len(content):
            text.append(HOLE)
            cursor += len(HOLE)
            holes += 1

    joined = "".join(text)
    form = "fstring" if any(p.is_f for p in parts) else "literal"
    if len(parts) > 1 and form == "literal":
        form = "concat"
    return PyString(
        file=file,
        text=joined,
        segments=segments,
        quote=parts[0].quote,
        line=node.lineno,  # type: ignore[attr-defined]
        key=key,
        form=form,
        node_start=start,
        node_end=end,
        holes=holes,
    )


def _runtime(node: ast.AST, source: bytes, offsets: list[int], file: str, key: str) -> PyString:
    start, end = _span(node, offsets)
    return PyString(
        file=file,
        text="",
        segments=[],
        quote="'",
        line=node.lineno,  # type: ignore[attr-defined]
        key=key,
        form="runtime",
        node_start=start,
        node_end=end,
    )


def _value(value: ast.AST, source: bytes, offsets: list[int], file: str, key: str) -> PyString | None:
    if isinstance(value, ast.Constant):
        if not isinstance(value.value, str):
            return None
        return _from_literal(value, source, offsets, file, key)
    if isinstance(value, ast.JoinedStr):
        return _from_literal(value, source, offsets, file, key)
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        # `'Failed: ' + str(e)` — reviewed as the sentence it forms, with the
        # literal half as the only writable segment.
        left = _value(value.left, source, offsets, file, key)
        if left is not None and left.form in ("literal", "concat", "fstring"):
            left.text = left.text + HOLE
            left.holes += 1
            left.form = "concat"
            return left
        return _runtime(value, source, offsets, file, key)
    return _runtime(value, source, offsets, file, key)


def extract_file(path: Path, repo_root: Path) -> list[PyString]:
    source = path.read_bytes()
    offsets = _line_offsets(source)
    rel = str(path.relative_to(repo_root))
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    found: list[PyString] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value in KEYS:
                    got = _value(v, source, offsets, rel, str(k.value))
                    if got is not None:
                        found.append(got)
        elif isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
            )
            if name != "jsonify":
                continue
            for kw in node.keywords:
                if kw.arg in KEYS:
                    got = _value(kw.value, source, offsets, rel, kw.arg)
                    if got is not None:
                        found.append(got)
    found.sort(key=lambda s: s.node_start)
    return found


def extract(repo_root: Path, subdir: str = "app/api") -> list[PyString]:
    out: list[PyString] = []
    for path in sorted((repo_root / subdir).glob("*.py")):
        out.extend(extract_file(path, repo_root))
    return out
