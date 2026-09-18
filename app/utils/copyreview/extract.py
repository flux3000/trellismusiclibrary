"""
extract.py — the orchestrator.

Runs stage A (jsparse) then stage B (htmlscan) over the frontend, walks the API
layer (pyapi), classifies with the oracles, labels with locate and group, and
collects the result into units.

The unit of review is the sentence a user reads, not the literal in the source.
One sentence can be spliced together from three string fragments and appear at
sixteen sites; that is one decision and sixteen splices, and conflating the two
is how a global replace ships.

Identity is `sha256(normalized text + section + scenario)` and never file and
line, because line numbers move whenever anyone edits anything above them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import group, htmlscan, jsparse, locate, pyapi
from .jsparse import Context, Literal, normalize
from .oracles import Oracles

JS_FILES = ("app.js", "api.js", "debug.js", "player.js", "main.js")
HTML_FILES = ("index.html",)

# A plural switch: `n === 1 ? '' : 's'`. Both outcomes are knowable, and a `{}`
# in the middle of a word reads as a typo rather than as a variant.
_PLURAL = re.compile(r"\?\s*(['\"])([^'\"]{0,4})\1\s*:\s*(['\"])([^'\"]{0,4})\3\s*$")
_SUBSTITUTION = re.compile(r"^\$\{(.*)\}$", re.S)

# The right-hand side of a comparison that reads as user input rather than as
# internal state. Measured 2026-09-17: Trellis has NO typed-confirmation guard
# — the plan's assumption that `'DELETE'` × 16 was one turned out to be the
# HTTP method in api.js, and deletes are confirmed with confirm() dialogs. The
# check stays because the hazard is real if one is ever added; it just finds
# nothing today, which is the correct answer.
_TYPED_INPUT = re.compile(r"\.value\b|\btrim\(\)|\binput\b|\bprompt\b|\btyped\b")
_MAX_VARIANTS = 4

# How many user-facing strings a guard token may appear in before it is
# treated as an ordinary English word rather than a token to protect.
_GUARD_RARITY = 3


@dataclass
class Site:
    file: str
    line: int
    kind: str  # html_text | html_attr | string | template | concat | py_error
    spans: list[tuple[int, int]]  # byte ranges covering the reviewed text
    node_start: int  # byte range of the whole expression, for removal
    node_end: int
    quote: str
    in_template: bool
    section: str
    scenario: str
    function: str
    page: str
    never_seen: bool
    raw: str = ""  # this site's own spelling, which can differ between sites
    attr: str | None = None
    attr_pair: list[tuple[int, int]] = field(default_factory=list)
    reason: str = ""
    holes: int = 0
    # The source expression each `{}` stood for, in order. The review card
    # shows these instead of inventing sample data: `${esc(artist.name)}` tells
    # you what lands in the gap, and a made-up name does not.
    hole_sources: list[str] = field(default_factory=list)
    form: str = "literal"  # pyapi's form, or 'literal' for JS


@dataclass
class Unit:
    key: str
    text: str  # normalized, escapes resolved — what a human reads
    raw: str  # reconstruction as it sits in the source
    section: str
    scenario: str
    page: str
    user_facing: bool
    never_seen: bool
    em_dash: bool
    variants: list[str] = field(default_factory=list)
    sites: list[Site] = field(default_factory=list)

    @property
    def occurrences(self) -> int:
        return len(self.sites)


@dataclass
class Extraction:
    units: list[Unit]
    file_hashes: dict[str, str]
    # Strings the code compares against: enum values, typed-confirmation
    # tokens, statuses. Write-back warns loudly before letting a replacement
    # drop one of these out of a sentence — `'DELETE'` sits at sixteen sites,
    # and rewording the instruction without the guard breaks delete
    # confirmation silently.
    guard_tokens: set[str] = field(default_factory=set)

    def to_json(self) -> dict:
        return {
            "file_hashes": self.file_hashes,
            "guard_tokens": sorted(self.guard_tokens),
            "units": [
                {**asdict(u), "sites": [asdict(s) for s in u.sites], "occurrences": u.occurrences}
                for u in self.units
            ],
        }

    # -- the prebuilt filters the plan asks for --------------------------
    def never_seen(self) -> list[Unit]:
        return [u for u in self.units if u.never_seen and u.user_facing]

    def em_dashed(self) -> list[Unit]:
        return [u for u in self.units if u.em_dash and u.user_facing]

    def reviewable(self) -> list[Unit]:
        """The queue: strings that speak to a user.

        The rest are extracted and kept so they can be filtered back in, but
        they are not what Ryan signed up to read.
        """
        return [u for u in self.units if u.user_facing]


def identity(text: str, section: str, scenario: str) -> str:
    payload = f"{normalize(text)}\x00{section}\x00{scenario}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def variants_of(raw: str, holes: list[jsparse.Hole]) -> list[str]:
    """Expand plural switches into the sentences a user actually sees."""
    expansions: list[tuple[int, tuple[str, str]]] = []
    for hole in holes:
        # `${n === 1 ? '' : 's'}` — strip the substitution's own wrapper, or
        # the end anchor never matches and no plural is ever expanded.
        inner = _SUBSTITUTION.match(hole.source.strip())
        match = _PLURAL.search((inner.group(1) if inner else hole.source).strip())
        if match:
            expansions.append((hole.recon_offset, (match.group(2), match.group(4))))
    if not expansions:
        return []

    # Substitute by OFFSET, descending, not by `replace`. A sentence can hold
    # a count and a plural — `{} recording{}` — and replacing the first `{}`
    # found turns that into "s recording{}", which is worse than leaving it
    # alone because it looks like real copy.
    out = [raw]
    for offset, (a, b) in sorted(expansions, reverse=True):
        grown: list[str] = []
        for text in out:
            head, tail = text[:offset], text[offset + len(jsparse.HOLE) :]
            grown.append(head + a + tail)
            grown.append(head + b + tail)
        out = grown[:_MAX_VARIANTS]
    seen: list[str] = []
    for text in out:
        shown = normalize(text)
        if shown not in seen:
            seen.append(shown)
    return seen if len(seen) > 1 else []


class _Collector:
    def __init__(self, oracles: Oracles) -> None:
        self.oracles = oracles
        self.units: dict[str, Unit] = {}
        self.guard_tokens: set[str] = set()

    def add(
        self,
        *,
        raw: str,
        kind: str,
        ctx: Context,
        site: Site,
        holes: list[jsparse.Hole],
    ) -> None:
        user_facing, reason = self.oracles.classify(raw, ctx, kind)
        shown = normalize(raw)
        if not shown:
            return
        site.reason = reason
        site.raw = raw
        if not site.hole_sources:
            site.hole_sources = [h.source for h in sorted(holes, key=lambda h: h.recon_offset)]
        if ctx.compared_with and _TYPED_INPUT.search(ctx.compared_with):
            # A literal compared against something the user TYPED: the
            # "type DELETE to confirm" shape. Only these are guard tokens.
            #
            # The first version collected every comparison operand, which
            # meant the entity-kind enums ('artist', 'recording') became
            # guards and the warning fired on any sentence containing those
            # words. A check that cries wolf gets muted.
            self.guard_tokens.add(normalize(raw))
        key = identity(raw, site.section, site.scenario)
        unit = self.units.get(key)
        if unit is None:
            unit = Unit(
                key=key,
                text=shown,
                raw=raw,
                section=site.section,
                scenario=site.scenario,
                page=site.page,
                user_facing=user_facing,
                never_seen=site.never_seen,
                em_dash=group.has_em_dash(raw),
                variants=variants_of(raw, holes),
            )
            self.units[key] = unit
        else:
            # A unit is user-facing if ANY of its sites is, and never-seen only
            # if EVERY site is. A string that also appears on a happy path has
            # been seen.
            unit.user_facing = unit.user_facing or user_facing
            unit.never_seen = unit.never_seen and site.never_seen
            if unit.page != site.page:
                unit.page = locate.SHARED
        unit.sites.append(site)


def _js_file(path: Path, repo_root: Path, oracles: Oracles, collector: _Collector) -> None:
    source = path.read_bytes()
    rel = str(path.relative_to(repo_root))
    root = jsparse.parse(source)

    banner_list = locate.banners(source)
    fns = locate.functions(root, source)
    reach = locate.pages_by_function(root, source, fns)

    def label(lit: Literal, byte: int) -> tuple[str, str]:
        owner = lit.ctx.owner_fn
        owner_start = owner.start_byte if owner is not None else byte
        name = locate.function_name(owner, source) if owner is not None else locate.MODULE
        return locate.section_at(owner_start, banner_list), name

    for lit in jsparse.literals(root, source, rel):
        section, fn_name = label(lit, lit.node_start)
        page = locate.page_label(reach.get(fn_name, set()))

        if htmlscan.looks_like_markup(lit.text):
            findings = htmlscan.scan(lit)
            if not findings:
                # Markup carrying no text and no visible attribute: a wrapper,
                # an icon, a grid of `${}` holes. It is not copy, and letting
                # it fall through to the prose heuristic classified whole
                # `<button class="…" data-flag="…">` strings as sentences.
                continue
            for finding in findings:
                ctx = lit.ctx
                scenario = group.scenario_of(finding.kind, ctx)
                if finding.kind == "html_attr":
                    scenario = group.AFFORDANCE
                collector.add(
                    raw=finding.text,
                    kind=finding.kind,
                    ctx=ctx,
                    # Hole offsets are relative to the whole literal, and the
                    # reviewed text is a run inside it — so rebase them, or a
                    # plural gets spliced two characters to the right and
                    # "Listening Quality" ships as "Listensg Quality".
                    holes=[
                        jsparse.Hole(h.recon_offset - finding.start, h.source)
                        for h in lit.holes
                        if finding.start <= h.recon_offset < finding.end
                    ],
                    site=Site(
                        file=rel,
                        line=lit.line + lit.text[: finding.start].count("\n"),
                        kind=finding.kind,
                        spans=lit.spans(finding.start, finding.end),
                        node_start=lit.node_start,
                        node_end=lit.node_end,
                        quote=lit.quote_at(finding.start),
                        in_template=lit.kind == "template",
                        section=section,
                        scenario=scenario,
                        function=fn_name,
                        page=page,
                        never_seen=group.never_seen(finding.kind, ctx, scenario),
                        attr=finding.attr,
                        attr_pair=(
                            lit.spans(finding.pair_start, finding.pair_end)
                            if finding.pair_start >= 0
                            else []
                        ),
                        holes=len(lit.holes),
                    ),
                )
            continue


        scenario = group.scenario_of(lit.kind, lit.ctx)
        collector.add(
            raw=lit.text,
            kind=lit.kind,
            ctx=lit.ctx,
            holes=lit.holes,
            site=Site(
                file=rel,
                line=lit.line,
                kind=lit.kind,
                spans=lit.spans(0, len(lit.text)),
                node_start=lit.node_start,
                node_end=lit.node_end,
                quote=lit.quote,
                in_template=lit.kind == "template",
                section=section,
                scenario=scenario,
                function=fn_name,
                page=page,
                never_seen=group.never_seen(lit.kind, lit.ctx, scenario),
                holes=len(lit.holes),
            ),
        )


def _html_file(path: Path, repo_root: Path, collector: _Collector) -> None:
    """index.html goes through stage B alone — it is markup, not a template."""
    text = path.read_text(encoding="utf-8", errors="replace")
    rel = str(path.relative_to(repo_root))
    source = path.read_bytes()
    line_of = _line_indexer(source)

    fake = Literal(
        file=rel,
        kind="template",
        text=text,
        segments=[jsparse.Segment(0, len(text), 0, len(source), text)],
        quote="`",
        node_start=0,
        node_end=len(source),
        line=1,
    )
    for finding in htmlscan.scan(fake):
        scenario = group.AFFORDANCE if finding.kind == "html_attr" else group.UI
        spans = fake.spans(finding.start, finding.end)
        collector.add(
            raw=finding.text,
            kind=finding.kind,
            ctx=Context(),
            holes=[],
            site=Site(
                file=rel,
                line=line_of(spans[0][0]) if spans else 1,
                kind=finding.kind,
                spans=spans,
                node_start=spans[0][0] if spans else 0,
                node_end=spans[-1][1] if spans else 0,
                quote='"',
                in_template=False,
                section=f"index.html <{finding.tag}>" if finding.tag else "index.html",
                scenario=scenario,
                function=locate.MODULE,
                page="Document shell",
                never_seen=False,
                attr=finding.attr,
                attr_pair=(
                    fake.spans(finding.pair_start, finding.pair_end)
                    if finding.pair_start >= 0
                    else []
                ),
            ),
        )


def _line_indexer(source: bytes):
    starts = [0]
    for i, byte in enumerate(source):
        if byte == 0x0A:
            starts.append(i + 1)

    def line_of(byte_offset: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= byte_offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    return line_of


def _api_layer(repo_root: Path, collector: _Collector) -> None:
    for found in pyapi.extract(repo_root):
        if found.form == "runtime":
            # No literal to review. Recorded so that a message whose text is
            # supplied at runtime is visibly accounted for rather than simply
            # absent from the count.
            collector.add(
                raw=f"(text supplied at runtime: {found.key})",
                kind="py_runtime",
                ctx=Context(),
                holes=[],
                site=Site(
                    file=found.file,
                    line=found.line,
                    kind="py_runtime",
                    spans=[],
                    node_start=found.node_start,
                    node_end=found.node_end,
                    quote=found.quote,
                    in_template=False,
                    section=found.file,
                    scenario=group.ERROR,
                    function=locate.MODULE,
                    page="API layer",
                    never_seen=True,
                    form=found.form,
                ),
            )
            continue
        collector.add(
            raw=found.text,
            kind="py_error",
            ctx=Context(),
            holes=[],
            site=Site(
                file=found.file,
                line=found.line,
                kind="py_error",
                spans=[(s.byte_start, s.byte_end) for s in found.segments],
                node_start=found.node_start,
                node_end=found.node_end,
                quote=found.quote,
                in_template=False,
                section=found.file,
                scenario=group.ERROR,
                function=locate.MODULE,
                page="API layer",
                never_seen=True,
                form=found.form,
                holes=found.holes,
            ),
        )


def extract(repo_root: Path) -> Extraction:
    repo_root = Path(repo_root)
    oracles = Oracles.load(repo_root)
    collector = _Collector(oracles)
    hashes: dict[str, str] = {}

    static = repo_root / "app" / "static"
    for name in JS_FILES:
        path = static / "js" / name
        if not path.exists():
            continue
        hashes[str(path.relative_to(repo_root))] = file_hash(path)
        _js_file(path, repo_root, oracles, collector)

    for name in HTML_FILES:
        path = static / name
        if not path.exists():
            continue
        hashes[str(path.relative_to(repo_root))] = file_hash(path)
        _html_file(path, repo_root, collector)

    for path in sorted((repo_root / "app" / "api").glob("*.py")):
        hashes[str(path.relative_to(repo_root))] = file_hash(path)
    _api_layer(repo_root, collector)

    units = sorted(
        collector.units.values(),
        key=lambda u: (u.page, u.section, u.sites[0].file, u.sites[0].line),
    )
    # A guard token has to be RARE in the copy to be worth warning about. A
    # real typed-confirmation token appears in one or two strings — the
    # instruction that tells you to type it. `typeof x === 'string'` and the
    # entity-kind enums also compare against user input in places, and those
    # words appear in dozens of sentences; warning on them would fire on
    # almost every replacement, and a check that cries wolf gets muted.
    facing = [u for u in units if u.user_facing]
    guards = set()
    for token in collector.guard_tokens:
        if not token or len(token) < 2:
            continue
        pattern = re.compile(rf"\b{re.escape(token)}\b")
        if sum(1 for u in facing if pattern.search(u.text)) <= _GUARD_RARITY:
            guards.add(token)

    return Extraction(units=units, file_hashes=hashes, guard_tokens=guards)
