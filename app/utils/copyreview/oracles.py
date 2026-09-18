"""
oracles.py — telling copy from code.

Every rule here is derived from the repo itself rather than from a list someone
maintains by hand, because a hand-maintained list of "things that are not copy"
rots the moment a class is renamed and then silently starts hiding real
sentences.

Four oracles, cheapest and most certain first:

1. CSS classes, parsed out of main.css.
2. Icon keys, read from the ICONS registry in app.js.
3. Syntactic position — a selector argument, an attribute NAME, an enum being
   compared, an object key. This is the highest-yield rule by a wide margin,
   and it is the only one that is exact: it does not guess from the text, it
   reads what the code does with it.
4. A prose heuristic, last and loosest.

Precision is around 80 to 85 percent, so the tool does not pretend otherwise:
whatever survives and is still not copy gets the "not user-facing" verdict
once, and the ledger remembers it. Classifying by hand once beats a fifth
heuristic that mis-files something real.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .jsparse import Context, display_text

# ── Oracle 3: what the code does with the literal ───────────────────────────

# Every argument is a selector or a class name.
SELECTOR_CALLS = {
    "querySelector",
    "querySelectorAll",
    "closest",
    "matches",
    "getElementById",
    "getElementsByClassName",
    "getElementsByTagName",
    "classList.add",
    "classList.remove",
    "classList.toggle",
    "classList.contains",
    "classList.replace",
}

# Only the named argument is code; later arguments can be copy. `setAttribute`
# is the one that matters: argument 0 is 'aria-label', argument 1 is the text a
# screen reader speaks.
NAME_ARGUMENT_ONLY = {
    "setAttribute": 0,
    "getAttribute": 0,
    "removeAttribute": 0,
    "hasAttribute": 0,
    "toggleAttribute": 0,
    "addEventListener": 0,
    "removeEventListener": 0,
    "insertAdjacentHTML": 0,
    "style.setProperty": 0,
    "style.removeProperty": 0,
    "localStorage.getItem": 0,
    "localStorage.setItem": 0,
    "localStorage.removeItem": 0,
    "sessionStorage.getItem": 0,
    "sessionStorage.setItem": 0,
    "fetch": 0,
    "api.get": 0,
    "api.post": 0,
    "api.put": 0,
    "api.patch": 0,
    "api.del": 0,
    "api.delete": 0,
    "icon": 0,
    "tic": 0,
}

# ── Oracle 4: shapes that are never a sentence ──────────────────────────────

_ALL_CAPS = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ONE_LOWER_TOKEN = re.compile(r"^[a-z][a-z0-9]*$")
_KEBAB_OR_SNAKE = re.compile(r"^[a-z][a-z0-9]*([-_][a-z0-9]+)+$")
_PATHISH = re.compile(r"^[./#]|^https?://|^data:|^blob:|^mailto:")
_HEX_COLOUR = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_SELECTORISH = re.compile(r"^[.#\[][\w\-\[\]='\"^$*~|. >+]+$")
_NUMERIC = re.compile(r"^[\d\s.,:%+\-/]*$")
_CSS_VALUE = re.compile(
    r"^-?\d*\.?\d+(px|rem|em|%|vh|vw|s|ms|deg|fr|ch)$|^(none|auto|block|flex|grid|hidden|"
    r"visible|inherit|initial|unset|inline|inline-block|absolute|relative|fixed|sticky|"
    r"center|left|right|top|bottom|row|column|nowrap|wrap|pointer|default|transparent)$"
)
_HTTP_VERB = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$")
_FILE_EXT = re.compile(r"^\.?\w{1,5}$")
_MIME = re.compile(r"^[a-z]+/[\w.+-]+$")
_HAS_LETTERS = re.compile(r"[A-Za-z]")
_WORD = re.compile(r"[A-Za-z]{2,}")
_SENTENCE_END = re.compile(r"[.!?:]$")
_LOWER_WORD_PAIR = re.compile(r"[a-z]{2,}\s+[a-z]{2,}")

# Terms whose presence marks a string as a machine token even though it reads
# like a word. Kept deliberately short; anything longer belongs in a verdict,
# not a stoplist.
_TOKENISH_WORDS = {
    "true",
    "false",
    "null",
    "undefined",
    "asc",
    "desc",
    "admin",
    "playback",
    "utf-8",
}

_URL_EXTENSIONS = {"woff", "woff2", "ttf", "otf", "svg", "png", "jpg", "jpeg", "webp", "gif", "ico"}


@dataclass
class Oracles:
    css_classes: set[str] = field(default_factory=set)
    icon_keys: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, repo_root: Path) -> "Oracles":
        return cls(
            css_classes=read_css_classes(repo_root / "app" / "static" / "css" / "main.css"),
            icon_keys=read_icon_keys(repo_root / "app" / "static" / "js" / "app.js"),
        )

    # -- the decision -----------------------------------------------------
    def classify(self, text: str, ctx: Context, kind: str) -> tuple[bool, str]:
        """Is this string something a user reads? Returns (verdict, reason).

        `kind` is the extraction kind: html_text, html_attr, string, template,
        concat, py_error.
        """
        shown = display_text(text).strip()
        if not shown:
            return False, "empty"

        # An API error value and a visible attribute are user-facing by
        # construction — position proves it, so no heuristic gets a vote.
        if kind in ("py_error", "html_attr"):
            return True, f"{kind} by position"

        if kind == "html_text":
            # Text between tags is rendered, so position settles it. The only
            # exclusions are runs with no word in them: a separator, a lone
            # letter used as a glyph, a bare count.
            if _NUMERIC.fullmatch(shown) or not _WORD.search(shown):
                return False, "no word"
            return True, "rendered text node"

        # Everything below is a bare literal, where position decides most of it.
        if ctx.is_object_key:
            return False, "object key"
        if ctx.compared_with is not None:
            return False, f"compared with {ctx.compared_with[:40]}"
        if ctx.call_name in SELECTOR_CALLS:
            return False, f"{ctx.call_name} argument"
        named_arg = NAME_ARGUMENT_ONLY.get(ctx.call_name or "")
        if named_arg is not None and ctx.call_arg_index == named_arg:
            return False, f"{ctx.call_name} name argument"

        if shown in self.icon_keys:
            return False, "icon key"
        tokens = shown.split()
        if tokens and all(t in self.css_classes for t in tokens):
            return False, "css class"

        return self._prose(shown)

    def _prose(self, shown: str) -> tuple[bool, str]:
        low = shown.lower()
        if low in _TOKENISH_WORDS:
            return False, "machine token"
        if _HEX_COLOUR.fullmatch(shown):
            return False, "colour"
        if _HTTP_VERB.fullmatch(shown):
            return False, "http verb"
        if _MIME.fullmatch(shown):
            return False, "mime type"
        if _PATHISH.match(shown) or _SELECTORISH.fullmatch(shown):
            return False, "path or selector"
        if _NUMERIC.fullmatch(shown) or not _HAS_LETTERS.search(shown):
            return False, "no letters"
        if _CSS_VALUE.fullmatch(low):
            return False, "css value"
        if " " not in shown:
            if _ALL_CAPS.fullmatch(shown):
                return False, "constant"
            if _KEBAB_OR_SNAKE.fullmatch(shown):
                return False, "kebab or snake token"
            if _ONE_LOWER_TOKEN.fullmatch(shown):
                return False, "lowercase token"
            if _FILE_EXT.fullmatch(shown) and low.lstrip(".") in _URL_EXTENSIONS:
                return False, "file extension"
            # A single capitalised word is usually a label: Recordings, Venue,
            # Artist. Those are copy — they are what a column header says.
            if shown[0].isupper() and len(shown) >= 3:
                return True, "capitalised label"
            return False, "single token"

        if _LOWER_WORD_PAIR.search(shown) or _SENTENCE_END.search(shown):
            return True, "prose"
        if shown[0].isupper():
            return True, "capitalised phrase"
        return False, "multi-token, no prose signal"


# ── Oracle 1: the stylesheet ────────────────────────────────────────────────

_CSS_CLASS = re.compile(r"\.(-?[_a-zA-Z][_a-zA-Z0-9-]*)")
_CSS_BLOCKS = re.compile(r"\{[^{}]*\}", re.S)
_CSS_COMMENTS = re.compile(r"/\*.*?\*/", re.S)


def read_css_classes(path: Path) -> set[str]:
    """Class tokens in main.css, from selector positions only.

    Declaration blocks are stripped first, or `url(font.woff2)` and every
    decimal length arrive as class names.
    """
    if not path.exists():
        return set()
    text = _CSS_COMMENTS.sub(" ", path.read_text(encoding="utf-8", errors="replace"))
    selectors = _CSS_BLOCKS.sub(" ", text)
    found = {m.group(1) for m in _CSS_CLASS.finditer(selectors)}
    return {c for c in found if c.lower() not in _URL_EXTENSIONS}


# ── Oracle 2: the icon registry ─────────────────────────────────────────────

_ICON_KEY = re.compile(r"^\s*([A-Za-z][\w-]*)\s*:", re.M)


def read_icon_keys(app_js: Path) -> set[str]:
    """Keys of the ICONS object in app.js.

    Read by locating the object and scanning its own braces rather than by
    importing anything — this has to work with no Node on the machine.
    """
    if not app_js.exists():
        return set()
    text = app_js.read_text(encoding="utf-8", errors="replace")
    at = text.find("const ICONS")
    if at < 0:
        return set()
    open_brace = text.find("{", at)
    if open_brace < 0:
        return set()
    depth = 0
    end = open_brace
    for i in range(open_brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = text[open_brace + 1 : end]
    # Only top-level keys: an icon's value is a string of path data, so nested
    # braces are not expected, but a comment can contain a colon.
    return {m.group(1) for m in _ICON_KEY.finditer(body)}
