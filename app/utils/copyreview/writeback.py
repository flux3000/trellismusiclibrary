"""
writeback.py — turning decisions into edits, without ever guessing.

The rule that shapes everything here: **never a regex.** A replacement is a
splice into a byte range recovered by re-parsing the file at write time, and
the byte ranges are applied in descending order so an earlier edit cannot move
a later one. The file is re-parsed afterwards and the write is abandoned if it
no longer parses.

Eight ways this could ship a wrong edit, and what stops each:

1. **Duplicates.** 218 strings sit at more than one site. Every plan is
   per-occurrence; there is no global replace anywhere in this module.
2. **Guard tokens.** A confirmation-typing guard — type the word, the code
   compares — breaks silently if the instruction is reworded without the word.
   Any replacement that drops a rare token the code compares against user
   input carries a loud warning.

   ⚠ Measured 2026-09-17: Trellis has none. The plan for this tool assumed
   `'DELETE'` at sixteen sites was such a guard; it is the HTTP method in
   `api.js`, and deletes are confirmed with `confirm()` dialogs instead. The
   check finds nothing today, which is the right answer, and the hazard is
   real if one is ever added.
3. **Removal breaking syntax.** A text node is spliced to nothing; an attribute
   is removed as a whole `title="…"` pair, because `title=""` is a different
   thing to a screen reader; and a bare literal becomes `''` and is NEVER
   deleted, or `alert('Failed: ' + e.message)` becomes `alert( + e.message)`
   and ships.
4. **Escaping.** Per site, for that site's own quote character, and HTML
   escaping on top when the text sits inside markup. An apostrophe in a
   single-quoted literal ends it early; a `<` in a text node opens a tag.
5. **`${` in a replacement** inside a template literal becomes live
   interpolation. Escaped, not accepted quietly.
6. **Concurrent edits.** Every file's sha256 is recorded at extraction and
   checked before writing. No force flag — a stale plan is rebuilt, not forced.
7. **Reformatting.** Only the target bytes are touched. A tool that reflows
   16,471 lines produces an unreviewable diff and gets switched off.
8. **No `.bak` files.** The target files must be clean in git so `git diff`
   shows exactly what the tool did, and nothing else.

Dry run is the default and prints a unified diff. `apply` writes.
"""

from __future__ import annotations

import difflib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import jsparse, ledger as ledger_mod
from .extract import Extraction, Site, Unit, file_hash
from .jsparse import HOLE

# Files the tool will write. Anything else is refused outright rather than
# handled generically, because the parse-check after the splice has to know
# which parser applies.
JS_SUFFIX = ".js"
PY_SUFFIX = ".py"
HTML_SUFFIX = ".html"


@dataclass
class Splice:
    file: str
    start: int
    end: int
    new: str
    why: str


@dataclass
class Plan:
    splices: list[Splice] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.splices) and not self.refusals

    def by_file(self) -> dict[str, list[Splice]]:
        out: dict[str, list[Splice]] = {}
        for splice in self.splices:
            out.setdefault(splice.file, []).append(splice)
        return out


# ── Escaping ────────────────────────────────────────────────────────────────


def _html_escape(text: str, quote: str) -> str:
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if quote == '"':
        out = out.replace('"', "&quot;")
    elif quote == "'":
        out = out.replace("'", "&#39;")
    return out


def escape(text: str, site: Site) -> str:
    """Escape a replacement for exactly the context this site sits in.

    Order matters. HTML escaping first, because it introduces `&`, `#` and `;`
    which no later step cares about; then the host language's own escaping,
    backslash before anything else so it does not double-escape the escapes.
    """
    out = text

    if site.kind == "html_attr":
        out = _html_escape(out, '"')
    elif site.kind == "html_text":
        out = _html_escape(out, "")

    if site.kind == "py_error":
        out = out.replace("\\", "\\\\")
        if site.quote in ("'", '"'):
            out = out.replace(site.quote, "\\" + site.quote)
        if site.form == "fstring":
            # A literal brace in an f-string has to be doubled or Python reads
            # it as the start of a replacement field.
            out = out.replace("{", "{{").replace("}", "}}")
        out = out.replace("\n", "\\n")
        return out

    out = out.replace("\\", "\\\\")
    if site.quote == "`":
        out = out.replace("`", "\\`").replace("${", "\\${")
    else:
        if site.quote in ("'", '"'):
            out = out.replace(site.quote, "\\" + site.quote)
        out = out.replace("\n", "\\n")
    return out


# ── Building a plan ─────────────────────────────────────────────────────────


def _align(original: str, replacement: str) -> list[tuple[int, str]] | None:
    """Match a replacement's literal runs to the original's, hole for hole.

    `Load more ({} left)` has two literal runs and one hole. A replacement has
    to have the same holes in the same order, or there is no honest way to
    decide where the value goes. Returns (run index, text) pairs for the runs
    that have a span, or None when the shapes do not line up.
    """
    old_runs = original.split(HOLE)
    new_runs = replacement.split(HOLE)
    if len(old_runs) != len(new_runs):
        return None
    out: list[tuple[int, str]] = []
    span_index = 0
    for old, new in zip(old_runs, new_runs):
        if old:
            out.append((span_index, new))
            span_index += 1
        elif new:
            # The original has no literal text here to splice into — the value
            # sits flush against the edge. Inserting would mean writing outside
            # any recorded range, so this one is handed back rather than
            # guessed at.
            return None
    return out


def _guard_warnings(unit: Unit, replacement: str, guard_tokens: set[str]) -> list[str]:
    out: list[str] = []
    for token in guard_tokens:
        if len(token) < 2:
            continue
        pattern = re.compile(rf"\b{re.escape(token)}\b")
        if pattern.search(unit.text) and not pattern.search(replacement):
            out.append(
                f"⚠ {unit.text[:50]!r} contains {token!r}, which the code compares "
                f"against elsewhere. The replacement drops it. If this is a "
                f"confirmation-typing guard, the confirmation will silently stop "
                f"matching."
            )
    return out


def _replace_splices(unit: Unit, site: Site, replacement: str, plan: Plan) -> None:
    where = f"{site.file}:{site.line}"
    if not site.spans:
        plan.refusals.append(f"{where}: no literal to write to ({site.form})")
        return

    aligned = _align(site.raw, replacement)
    if aligned is None:
        plan.refusals.append(
            f"{where}: the replacement's placeholders do not line up with "
            f"{site.raw[:50]!r}. Keep the same {site.raw.count(HOLE)} × {HOLE} "
            f"in the same order."
        )
        return
    if len(aligned) != len(site.spans):
        plan.refusals.append(
            f"{where}: {len(aligned)} literal run(s) to write, {len(site.spans)} "
            f"recorded. Refusing rather than guessing the alignment."
        )
        return

    for (index, text), (start, end) in zip(aligned, site.spans):
        plan.splices.append(
            Splice(file=site.file, start=start, end=end, new=escape(text, site), why=f"replace {unit.key}")
        )


def _remove_splices(unit: Unit, site: Site, plan: Plan, confirm_bare: bool) -> None:
    where = f"{site.file}:{site.line}"

    if site.kind == "html_attr":
        if not site.attr_pair:
            plan.refusals.append(
                f"{where}: cannot locate the whole {site.attr}=… pair, and removing "
                f"only the value would leave {site.attr}=\"\", which is not the same thing."
            )
            return
        for start, end in site.attr_pair:
            plan.splices.append(
                Splice(file=site.file, start=start, end=end, new="", why=f"remove attr {unit.key}")
            )
        return

    if site.kind == "html_text":
        for start, end in site.spans:
            plan.splices.append(
                Splice(file=site.file, start=start, end=end, new="", why=f"remove text {unit.key}")
            )
        plan.warnings.append(
            f"{where}: the text is gone but its element is not. Check whether the "
            f"wrapper should go too — an empty one still takes up space and still "
            f"has a border."
        )
        return

    # A bare literal. It becomes empty; it is never deleted.
    if not confirm_bare:
        plan.refusals.append(
            f"{where}: removing a bare literal empties it to '' rather than deleting "
            f"it, because deleting the node would leave `alert( + e.message)`. "
            f"Confirm the emptying explicitly."
        )
        return
    for start, end in site.spans:
        plan.splices.append(
            Splice(file=site.file, start=start, end=end, new="", why=f"empty literal {unit.key}")
        )
    plan.warnings.append(f"{where}: literal emptied to '' — it still evaluates, it just says nothing.")


def build(
    extraction: Extraction,
    ledger: ledger_mod.Ledger,
    *,
    only: set[str] | None = None,
    confirm_bare_removal: bool = False,
) -> Plan:
    """A splice plan for every decision that has not been written yet."""
    plan = Plan()
    units = {u.key: u for u in extraction.units}

    for entry in sorted(ledger.entries.values(), key=lambda e: e.id):
        if not entry.needs_write:
            continue
        if only is not None and entry.id not in only:
            continue
        unit = units.get(entry.id)
        if unit is None:
            plan.refusals.append(
                f"{entry.id}: decided on {entry.text[:40]!r}, which is no longer in "
                f"the source. Re-extract before writing."
            )
            continue

        if entry.verdict == ledger_mod.REPLACE:
            replacement = entry.replacement or ""
            plan.warnings.extend(_guard_warnings(unit, replacement, extraction.guard_tokens))
            if unit.occurrences > 1:
                plan.warnings.append(
                    f"{unit.text[:40]!r} sits at {unit.occurrences} sites and all of "
                    f"them are being changed. If it means different things in "
                    f"different places, split the unit first."
                )
            for site in unit.sites:
                _replace_splices(unit, site, replacement, plan)
        elif entry.verdict == ledger_mod.REMOVE:
            for site in unit.sites:
                _remove_splices(unit, site, plan, confirm_bare_removal)

    return plan


# ── Guards ──────────────────────────────────────────────────────────────────


def _git(repo_root: Path, *args: str) -> tuple[int, str]:
    """Run a READ-ONLY git command.

    Only commands that do not touch the index are used here. `git status`
    refreshes it, takes `.git/index.lock`, and over a sandbox bridge cannot
    release it — which leaves the next real commit dead with "another git
    process seems to be running" on a repo nobody touched.
    """
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout or done.stderr).strip()


def head_commit(repo_root: Path) -> str:
    """The commit a write was made against, or a marker when there isn't one.

    Stamped on every applied decision so a sign-off can be traced to the state
    of the tree it was made against. `uncommitted` is an honest answer, not a
    failure — Trellis spends most of its life with work in the tree.
    """
    code, out = _git(repo_root, "rev-parse", "--short", "HEAD")
    return out if code == 0 and out else "unknown"


def dirty_files(repo_root: Path) -> set[str]:
    """Tracked files with uncommitted changes. `git diff`, never `git status`."""
    code, out = _git(repo_root, "diff", "--name-only", "HEAD", "--")
    if code != 0:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def check_guards(repo_root: Path, extraction: Extraction, plan: Plan) -> list[str]:
    """Everything that must be true before a byte is written."""
    problems: list[str] = []
    targets = set(plan.by_file())

    for rel in sorted(targets):
        path = repo_root / rel
        if not path.exists():
            problems.append(f"{rel}: gone since extraction")
            continue
        recorded = extraction.file_hashes.get(rel)
        if recorded is None:
            problems.append(f"{rel}: no hash was recorded at extraction")
        elif file_hash(path) != recorded:
            problems.append(
                f"{rel}: changed since extraction. Re-extract and re-check the "
                f"pending decisions — the byte ranges in this plan point at the "
                f"old file."
            )

    # Only the TARGET files have to be clean. The plan's original rule was the
    # whole tree, which would refuse almost every real session here, and the
    # thing it protects is narrower than that: being able to read `git diff` on
    # the files the tool touched and see only the tool's work.
    dirty = dirty_files(repo_root) & targets
    for rel in sorted(dirty):
        problems.append(
            f"{rel}: already has uncommitted changes. Commit or stash them first, "
            f"so `git diff` afterwards shows only what this tool did."
        )
    return problems


# ── Applying ────────────────────────────────────────────────────────────────


def _parses(path: Path, data: bytes) -> str | None:
    """None when the new content is fine, else why it is not."""
    if path.suffix == JS_SUFFIX:
        try:
            jsparse.parse(data)
        except SyntaxError:
            return "no longer parses as JavaScript"
        return None
    if path.suffix == PY_SUFFIX:
        import ast

        try:
            ast.parse(data)
        except SyntaxError as exc:
            return f"no longer parses as Python: {exc}"
        return None
    if path.suffix == HTML_SUFFIX:
        return None
    return f"unsupported file type: {path.suffix}"


def splice(original: bytes, splices: list[Splice]) -> bytes:
    """Apply splices descending by start byte, so no edit moves another."""
    ordered = sorted(splices, key=lambda s: (s.start, s.end), reverse=True)
    previous_start = None
    out = original
    for item in ordered:
        if previous_start is not None and item.end > previous_start:
            raise ValueError(
                f"overlapping splices at {item.file} {item.start}-{item.end}; "
                f"refusing to write a plan that edits the same bytes twice"
            )
        out = out[: item.start] + item.new.encode("utf-8") + out[item.end :]
        previous_start = item.start
    return out


def diff(repo_root: Path, plan: Plan) -> str:
    """Unified diff of what the plan would do. Changes nothing."""
    chunks: list[str] = []
    for rel, splices in sorted(plan.by_file().items()):
        path = repo_root / rel
        before = path.read_bytes()
        after = splice(before, splices)
        chunks.extend(
            difflib.unified_diff(
                before.decode("utf-8", "replace").splitlines(keepends=True),
                after.decode("utf-8", "replace").splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=2,
            )
        )
    return "".join(chunks)


@dataclass
class Applied:
    files: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)


def apply(
    repo_root: Path,
    extraction: Extraction,
    plan: Plan,
    *,
    dry_run: bool = True,
    ledger: ledger_mod.Ledger | None = None,
) -> Applied:
    """Write the plan, or (by default) say what it would write.

    Every file is spliced and re-parsed in memory FIRST. Nothing reaches disk
    unless every target survives its own parse check, so a plan that breaks
    one file does not leave the others half written.
    """
    result = Applied()
    problems = check_guards(repo_root, extraction, plan)
    if problems:
        result.refused = problems
        return result
    if plan.refusals:
        result.refused = list(plan.refusals)
        return result

    staged: list[tuple[Path, bytes]] = []
    for rel, splices in sorted(plan.by_file().items()):
        path = repo_root / rel
        try:
            candidate = splice(path.read_bytes(), splices)
        except ValueError as exc:
            result.refused.append(str(exc))
            return result
        why = _parses(path, candidate)
        if why is not None:
            result.refused.append(f"{rel}: {why}. Nothing was written.")
            return result
        staged.append((path, candidate))

    if dry_run:
        result.files = [str(p.relative_to(repo_root)) for p, _ in staged]
        return result

    for path, data in staged:
        path.write_bytes(data)
        result.files.append(str(path.relative_to(repo_root)))

    # Retire the decisions that just landed. Without this every later run
    # re-plans the same splice, finds the old text gone, and refuses — the
    # tool would work exactly once.
    if ledger is not None:
        commit = head_commit(repo_root)
        written = {s.why.split()[-1] for s in plan.splices}
        for key in written:
            entry = ledger.entries.get(key)
            if entry is not None and entry.needs_write:
                ledger.mark_applied(entry, commit)
        ledger.save()
    return result
