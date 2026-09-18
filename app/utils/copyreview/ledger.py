"""
ledger.py — the decisions, and the only hard problem in this tool.

Extraction is cheap and can be re-run whenever. What must survive re-running is
Ryan's judgement, and it has to survive the code moving underneath it: a
function renamed, a block reindented, a section split in two. So identity is
never file and line.

Three tiers, and the third one is the whole mechanism:

1. **Exact** — `sha256(normalized text + section + scenario)`. Most matches.
2. **Text only, section changed** — the same sentence has moved. The decision
   is carried forward and flagged `moved`, because a sentence that was right in
   the ingest wizard is still right after someone renames the banner above it.
3. **Same section and scenario, small edit distance** — the decision is **not**
   carried. It comes back as `changed`, with a diff against what was approved.

Tier 3 is why a one-word edit to approved copy re-enters the queue by itself.
Carrying it forward would mean an agent could reword approved text and the
approval would silently cover the new wording, which is exactly the thing this
tool exists to prevent.

Stored as committed JSON rather than SQLite because the decisions belong in
git, next to the code they describe, and `db/*.db` is gitignored — SQLite would
put Ryan's sign-offs outside version control. Same call as
`tools/quality/labelled_corpus.json`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from .extract import Extraction, Unit, identity
from .jsparse import normalize

APPROVE = "approve"
REPLACE = "replace"
REMOVE = "remove"
NOT_USER_FACING = "not_user_facing"
VERDICTS = (APPROVE, REPLACE, REMOVE, NOT_USER_FACING)

# Status of a unit as of the last extraction.
REVIEWED = "reviewed"
MOVED = "moved"
CHANGED = "changed"
NEW = "new"
PENDING_WRITE = "pending_write"

# Tier 3 asks "is this approved copy that someone reworded?". For a sentence
# that is a useful question. For a one-word label it is not: 'Recording' and
# 'Recordings' are 0.95 similar and are two different labels, so approving one
# would send the other back for re-review as a rewording of it. Nearness
# therefore only applies above a length where a near-match means something;
# shorter strings fall through to "new", which is the safe answer because it
# just means somebody reads them.
_SIMILAR_ENOUGH = 0.80
_MIN_LENGTH_FOR_NEARNESS = 20

LEDGER_PATH = Path("app/utils/copyreview/ledger/copy_ledger.json")
BASELINE_PATH = Path("app/utils/copyreview/ledger/baseline.json")


@dataclass
class Entry:
    id: str
    text: str  # the normalized text the decision was made about
    verdict: str
    section: str
    scenario: str
    replacement: str | None = None
    decided_at: str = ""
    applied_commit: str | None = None  # None while a replacement is unwritten
    last_seen: str = ""
    note: str = ""
    # The sentence this one replaced, when it replaced one. The audit trail.
    previous_text: str = ""

    @property
    def needs_write(self) -> bool:
        return self.verdict in (REPLACE, REMOVE) and not self.applied_commit


@dataclass
class Match:
    unit: Unit
    entry: Entry | None
    status: str
    was: str | None = None  # the approved text, when status is 'changed'


@dataclass
class Ledger:
    path: Path
    entries: dict[str, Entry] = field(default_factory=dict)

    # -- io ---------------------------------------------------------------
    @classmethod
    def load(cls, repo_root: Path) -> "Ledger":
        path = Path(repo_root) / LEDGER_PATH
        ledger = cls(path=path)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("entries", []):
                ledger.entries[item["id"]] = Entry(**item)
        return ledger

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "entries": [asdict(e) for e in sorted(self.entries.values(), key=lambda e: e.id)],
        }
        # Two spaces and a trailing newline so a decision is a one-line diff.
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # -- decisions --------------------------------------------------------
    def decide(
        self,
        unit: Unit,
        verdict: str,
        replacement: str | None = None,
        note: str = "",
    ) -> Entry:
        if verdict not in VERDICTS:
            raise ValueError(f"unknown verdict: {verdict}")
        if verdict == REPLACE and not (replacement or "").strip():
            raise ValueError("a replacement verdict needs replacement text")
        entry = Entry(
            id=unit.key,
            text=unit.text,
            verdict=verdict,
            section=unit.section,
            scenario=unit.scenario,
            replacement=replacement if verdict == REPLACE else None,
            decided_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            last_seen=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            note=note,
        )
        self.entries[entry.id] = entry
        return entry

    def undo(self, unit_key: str) -> None:
        self.entries.pop(unit_key, None)

    def mark_applied(self, entry: Entry, commit: str) -> Entry:
        """Retire a written decision and re-file it against the NEW text.

        This is the step that stops the tool from writing the same edit twice.
        Once a replacement is in the source, the old sentence does not exist
        any more, so a decision about it is meaningless — and its identity hash
        (which includes the text) no longer matches anything. Left alone, the
        entry stays `needs_write` forever and every later run refuses it with
        "no longer in the source".

        So the entry becomes what it now means: the new text is approved, by
        the person who wrote it, at this commit. `previous_text` keeps the
        sentence it replaced, because that history is the whole audit trail.
        """
        self.entries.pop(entry.id, None)
        if entry.verdict == REMOVE:
            # Nothing to re-file against: the string is gone. The entry is
            # kept as the record that it was deliberately removed.
            entry.applied_commit = commit
            self.entries[entry.id] = entry
            return entry

        new_text = normalize(entry.replacement or entry.text)
        moved = Entry(
            id=identity(new_text, entry.section, entry.scenario),
            text=new_text,
            verdict=APPROVE,
            section=entry.section,
            scenario=entry.scenario,
            replacement=None,
            decided_at=entry.decided_at,
            applied_commit=commit,
            last_seen=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            note=entry.note,
            previous_text=entry.text,
        )
        self.entries[moved.id] = moved
        return moved

    # -- matching ---------------------------------------------------------
    def match(self, extraction: Extraction) -> list[Match]:
        by_text: dict[str, list[Entry]] = {}
        by_group: dict[tuple[str, str], list[Entry]] = {}
        for entry in self.entries.values():
            by_text.setdefault(entry.text, []).append(entry)
            by_group.setdefault((entry.section, entry.scenario), []).append(entry)

        out: list[Match] = []
        for unit in extraction.units:
            entry = self.entries.get(unit.key)
            if entry is not None:
                status = PENDING_WRITE if entry.needs_write else REVIEWED
                out.append(Match(unit=unit, entry=entry, status=status))
                continue

            # Tier 2: the same sentence, somewhere else.
            moved = by_text.get(unit.text)
            if moved:
                out.append(Match(unit=unit, entry=moved[0], status=MOVED))
                continue

            # Tier 3: near-miss in the same place — a rewording of approved
            # copy. Deliberately NOT carried forward.
            candidate = self._nearest(unit, by_group.get((unit.section, unit.scenario), []))
            if candidate is not None:
                out.append(Match(unit=unit, entry=None, status=CHANGED, was=candidate.text))
                continue

            out.append(Match(unit=unit, entry=None, status=NEW))
        return out

    @staticmethod
    def _nearest(unit: Unit, candidates: list[Entry]) -> Entry | None:
        if len(unit.text) < _MIN_LENGTH_FOR_NEARNESS:
            return None
        best: Entry | None = None
        best_ratio = 0.0
        for entry in candidates:
            if len(entry.text) < _MIN_LENGTH_FOR_NEARNESS:
                continue
            ratio = SequenceMatcher(None, entry.text, unit.text).ratio()
            if ratio > best_ratio:
                best, best_ratio = entry, ratio
        return best if best_ratio >= _SIMILAR_ENOUGH else None

    def touch(self, matches: list[Match]) -> None:
        """Record that a decision's string is still present."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for match in matches:
            if match.entry is not None and match.status in (REVIEWED, MOVED, PENDING_WRITE):
                match.entry.last_seen = now

    def orphans(self, extraction: Extraction) -> list[Entry]:
        """Decisions whose string is no longer anywhere in the source.

        Kept, not deleted: a string that comes back should come back already
        approved, and a decision is cheap to store.
        """
        live = {u.key for u in extraction.units} | {u.text for u in extraction.units}
        return [e for e in self.entries.values() if e.id not in live and e.text not in live]


# ── The gate's baseline ─────────────────────────────────────────────────────


@dataclass
class Baseline:
    """The backlog the gate ignores, and only ever shrinks.

    Day one there are several hundred unreviewed strings. A gate that fails on
    all of them fails red immediately and gets muted, which is how the first
    version of the undefined-names checker nearly died. So the backlog is
    written down once and the gate asserts "nothing NEW is unreviewed" until
    the list is empty, at which point it asserts "nothing is unreviewed".

    It is a ratchet, and it is a backstop. New user-facing text is not
    something to add and let the gate catch — it is something to get approved
    before it ships.
    """

    path: Path
    keys: set[str] = field(default_factory=set)
    created_at: str = ""

    @classmethod
    def load(cls, repo_root: Path) -> "Baseline":
        path = Path(repo_root) / BASELINE_PATH
        if not path.exists():
            return cls(path=path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(path=path, keys=set(raw.get("keys", [])), created_at=raw.get("created_at", ""))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": self.created_at
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": (
                "Strings that were already in the tree when the copy review gate "
                "was added. The gate ignores these and fails on anything else "
                "unreviewed. Remove keys as they are reviewed; never add one."
            ),
            "keys": sorted(self.keys),
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def prune(self, ledger: Ledger) -> int:
        """Drop keys that now have a real decision. Returns how many went."""
        gone = {k for k in self.keys if k in ledger.entries}
        self.keys -= gone
        return len(gone)


def unreviewed(matches: list[Match], baseline: Baseline) -> list[Match]:
    """What the gate fails on: user-facing, undecided, and not in the baseline."""
    out = []
    for match in matches:
        if not match.unit.user_facing:
            continue
        if match.status in (REVIEWED, MOVED, PENDING_WRITE):
            continue
        if match.unit.key in baseline.keys:
            continue
        out.append(match)
    return out


def identity_for(text: str, section: str, scenario: str) -> str:
    """Exposed so a caller can check a string without a full extraction."""
    return identity(normalize(text), section, scenario)
