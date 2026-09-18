"""
htmlscan.py — stage B of extraction: the markup inside the strings.

Anything stage A reconstructed that contains both `<` and `>` is markup, and
the copy in it is text nodes and a short list of visible attributes. The
fragments are unbalanced by nature — a template literal opens a div and closes
it three functions later — which is fine for a streaming parser and is why
`html.parser` is the right tool rather than a DOM.

Two exclusions carry their weight:

* The SVG element names. Lucide's paths are vendored verbatim into `ICONS`, and
  without this list the output drowns in path data.
* `value`. It is an attribute a user reads, but it is also every form's bound
  data, so including it swamps the queue with record fields. Left out on
  purpose; an input's user-facing text is its `placeholder`.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser

from .jsparse import HOLE, Literal

SKIP_TAGS = {
    "svg",
    "path",
    "rect",
    "circle",
    "line",
    "polyline",
    "polygon",
    "g",
    "defs",
    "use",
    "style",
    "script",
}

VISIBLE_ATTRS = ("title", "placeholder", "aria-label", "alt")


@dataclass
class Finding:
    """One run of copy inside a markup fragment, in reconstruction offsets."""

    kind: str  # 'html_text' | 'html_attr'
    text: str  # raw, escapes intact (see jsparse's note)
    start: int
    end: int
    tag: str  # enclosing element for text, owning element for an attribute
    attr: str | None = None
    # The whole `title="Close"` pair. Removing an attribute means deleting the
    # pair; deleting only the value leaves `title=""`, which is a different
    # thing to a screen reader.
    pair_start: int = -1
    pair_end: int = -1
    value_quote: str = '"' 


def looks_like_markup(text: str) -> bool:
    return "<" in text and ">" in text


class _Scanner(HTMLParser):
    def __init__(self) -> None:
        # convert_charrefs would rewrite `&times;` into a single character and
        # every offset after it would be wrong. Entities are stitched back into
        # the surrounding text run instead.
        super().__init__(convert_charrefs=False)
        self.findings: list[Finding] = []
        self._line_starts: list[int] = []
        self._stack: list[str] = []
        self._skip_depth = 0
        self._run: list[str] = []
        self._run_start: int | None = None

    # -- offsets ----------------------------------------------------------
    def feed_text(self, text: str) -> None:
        self._line_starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self._line_starts.append(i + 1)
        self.feed(text)
        self.close()
        self._flush()

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    # -- text runs --------------------------------------------------------
    def _flush(self) -> None:
        if self._run_start is None:
            return
        raw = "".join(self._run)
        start, end = self._run_start, self._run_start + len(raw)
        self._run = []
        self._run_start = None
        if self._skip_depth:
            return
        stripped = raw.strip()
        if not stripped or stripped == HOLE:
            return
        # Trim surrounding whitespace off the recorded span so the replaceable
        # range is the sentence and not the template's indentation.
        lead = len(raw) - len(raw.lstrip())
        trail = len(raw) - len(raw.rstrip())
        self.findings.append(
            Finding(
                kind="html_text",
                text=raw[lead : len(raw) - trail],
                start=start + lead,
                end=end - trail,
                tag=self._stack[-1] if self._stack else "",
            )
        )

    def _accumulate(self, raw: str) -> None:
        if self._run_start is None:
            self._run_start = self._offset()
        self._run.append(raw)

    def handle_data(self, data: str) -> None:
        self._accumulate(data)

    def handle_entityref(self, name: str) -> None:
        self._accumulate(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._accumulate(f"&#{name};")

    # -- tags -------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._flush()
        self._collect_attrs(tag, attrs)
        self._stack.append(tag)
        if tag in SKIP_TAGS:
            self._skip_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._flush()
        self._collect_attrs(tag, attrs)

    # A comment, doctype or processing instruction ends a text run. Without
    # these the run keeps accumulating across the comment while its recorded
    # span does not, so the span stops reproducing its own text — caught by
    # the round-trip assertion in tests/test_copy_reviewed.py.
    def handle_comment(self, data: str) -> None:
        self._flush()

    def handle_decl(self, decl: str) -> None:
        self._flush()

    def handle_pi(self, data: str) -> None:
        self._flush()

    def unknown_decl(self, data: str) -> None:
        self._flush()

    def handle_endtag(self, tag: str) -> None:
        self._flush()
        if tag in self._stack:
            while self._stack:
                popped = self._stack.pop()
                if popped in SKIP_TAGS:
                    self._skip_depth = max(0, self._skip_depth - 1)
                if popped == tag:
                    break

    def _collect_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth:
            return
        wanted = [(n, v) for n, v in attrs if n in VISIBLE_ATTRS and v and v.strip()]
        if not wanted:
            return
        raw_tag = self.get_starttag_text() or ""
        tag_start = self._offset()
        for name, value in wanted:
            if value is None or value.strip() == HOLE:
                continue
            span = _find_attr_value(raw_tag, name, value)
            if span is None:
                continue
            pair = _pair_span(raw_tag, name, span)
            quote = raw_tag[span[0] - 1] if span[0] > 0 and raw_tag[span[0] - 1] in "\"'" else ""
            self.findings.append(
                Finding(
                    kind="html_attr",
                    text=value,
                    start=tag_start + span[0],
                    end=tag_start + span[1],
                    tag=tag,
                    attr=name,
                    pair_start=tag_start + pair[0],
                    pair_end=tag_start + pair[1],
                    value_quote=quote,
                )
            )


def _find_attr_value(raw_tag: str, name: str, value: str) -> tuple[int, int] | None:
    """Locate an attribute's value inside the raw start-tag text.

    html.parser hands over parsed values with no positions, and a position is
    the whole point here. Searching for the value after its own attribute name
    keeps `title="Close"` apart from `aria-label="Close"` on the same element.
    """
    lowered = raw_tag.lower()
    search_from = 0
    while True:
        at = lowered.find(name.lower(), search_from)
        if at < 0:
            return None
        after = at + len(name)
        # Must be a whole attribute name followed by '='.
        if at > 0 and (raw_tag[at - 1].isalnum() or raw_tag[at - 1] in "-_"):
            search_from = after
            continue
        rest = raw_tag[after:]
        stripped = rest.lstrip()
        if not stripped.startswith("="):
            search_from = after
            continue
        eq = after + (len(rest) - len(stripped))
        val_region = raw_tag[eq + 1 :]
        offset = eq + 1 + (len(val_region) - len(val_region.lstrip()))
        if offset < len(raw_tag) and raw_tag[offset] in "\"'":
            start = offset + 1
            end = raw_tag.find(raw_tag[offset], start)
            if end < 0:
                return None
        else:
            start = offset
            end = start
            while end < len(raw_tag) and not raw_tag[end].isspace() and raw_tag[end] not in "/>":
                end += 1
        if raw_tag[start:end] == value:
            return (start, end)
        search_from = end


def _pair_span(raw_tag: str, name: str, value_span: tuple[int, int]) -> tuple[int, int]:
    """The full `name="value"` run, plus one leading space if there is one."""
    start = raw_tag.lower().rfind(name.lower(), 0, value_span[0])
    if start < 0:
        return value_span
    end = value_span[1]
    if end < len(raw_tag) and raw_tag[end] in "\"'":
        end += 1
    while start > 0 and raw_tag[start - 1] == " ":
        start -= 1
    return (start, end)


def scan(literal: Literal) -> list[Finding]:
    """Findings for one markup-bearing reconstruction, in offset order."""
    scanner = _Scanner()
    try:
        scanner.feed_text(literal.text)
    except Exception:
        # A fragment html.parser cannot cope with is reported as nothing found
        # rather than aborting the run. It then falls through to the bare
        # literal path, where it is at worst an extra row to classify once.
        return []
    return sorted(scanner.findings, key=lambda f: f.start)
