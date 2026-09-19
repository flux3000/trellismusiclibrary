"""
tests/test_copy_reviewed.py — no unreviewed copy slips in.

WHY

Every string Trellis shows a user is Ryan's to write or approve (2026-09-17).
An agent may propose copy; it does not ship it. This test is the BACKSTOP for
text that got in anyway. It is not a licence: "the gate will catch it" is not a
reason to add an unreviewed string and move on.

It re-extracts on every run and matches what it finds against
`app/utils/copyreview/ledger/copy_ledger.json`. The extraction, the ledger and
the matching are all the same code the review tool and `--report` use, so the
three can never disagree about what has been signed off.

A RATCHET, NOT A CLIFF

On the day this was added there were several hundred unreviewed strings in the
tree. A gate that fails on all of them fails red immediately and gets muted,
and then it is not there when it matters — the same way the first version of
`test_no_undefined_names.py` nearly died of sixteen false positives. So the
day-one backlog is written once into `ledger/baseline.json` and this test
ignores exactly those keys. Everything else unreviewed fails.

The baseline only ever shrinks: deciding a string removes its key
automatically, and nothing adds one. When it empties, the assertion becomes
"nothing unreviewed at all" on its own, with no code change.

    python3 -m app.utils.copyreview --baseline    # write it, once
    python3 -m app.utils.copyreview --report      # the same picture, outside pytest

ALSO ASSERTED: that every recorded byte range still reproduces its own text.
That is the invariant write-back rests on. If it ever fails, the extractor has
a mapping bug and the write path must not be trusted, because a span that does
not reproduce its text will splice a replacement over the wrong bytes.

No network, no audio, no library mount, no database. It parses files.
"""

from pathlib import Path

import pytest

from app.utils.copyreview.extract import extract
from app.utils.copyreview.ledger import Baseline, Ledger, unreviewed

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def extraction():
    return extract(REPO_ROOT)


def test_every_recorded_span_reproduces_its_own_text(extraction):
    """The invariant write-back depends on: a span still says what it said.

    Held separately from the review gate because it is about the extractor
    rather than about anyone's judgement. A failure here means the byte ranges
    are wrong, and a wrong byte range is a wrong edit.
    """
    sources: dict[str, bytes] = {}
    problems = []
    for unit in extraction.units:
        for site in unit.sites:
            if not site.spans:
                continue
            data = sources.setdefault(site.file, (REPO_ROOT / site.file).read_bytes())
            got = "".join(data[a:b].decode("utf-8", "replace") for a, b in site.spans)
            # The spans cover the literal runs only, so the holes drop out.
            expected = site.raw.replace("{}", "")
            if got != expected:
                problems.append(
                    f"{site.file}:{site.line} ({site.kind})\n"
                    f"      recorded {expected!r}\n"
                    f"      source   {got!r}"
                )
    assert not problems, (
        "Recorded byte ranges no longer reproduce their own text. The "
        "extractor has a mapping bug and write-back must not be used until it "
        "is fixed:\n    " + "\n    ".join(problems[:20])
    )


@pytest.mark.skip(reason=(
    "copy review gate is off (Ryan, 2026-09-18): unreviewed copy is not a priority right now. The tool still works: python3 -m app.utils.copyreview --report. Delete the skips to re-arm."
))
def test_no_unreviewed_user_facing_copy(extraction):
    ledger = Ledger.load(REPO_ROOT)
    baseline = Baseline.load(REPO_ROOT)
    matches = ledger.match(extraction)
    open_items = unreviewed(matches, baseline)

    if not open_items:
        return

    lines = []
    for match in sorted(open_items, key=lambda m: (m.unit.sites[0].file, m.unit.sites[0].line)):
        site = match.unit.sites[0]
        detail = f" (was {match.was!r})" if match.was else ""
        lines.append(
            f"{site.file}:{site.line}  [{match.unit.scenario}]  {site.section}\n"
            f"      {match.unit.text!r}{detail}"
        )

    pytest.fail(
        f"{len(open_items)} user-facing string(s) have no sign-off.\n\n"
        "Every string a user reads is approved by a person before it ships, so "
        "this is not a formality to baseline away. Review them:\n\n"
        "    python3 tools/copy_review.py\n\n"
        + "\n".join(lines[:40])
        + (f"\n    ... and {len(open_items) - 40} more" if len(open_items) > 40 else "")
    )


@pytest.mark.skip(reason=(
    "copy review gate is off (Ryan, 2026-09-18): unreviewed copy is not a priority right now. The tool still works: python3 -m app.utils.copyreview --report. Delete the skips to re-arm."
))
def test_baseline_only_shrinks(extraction):
    """A key in the baseline that is also decided is stale, not a conflict.

    Catches the one way the ratchet can slip backwards: a baseline regenerated
    over the top of real decisions, which would quietly re-exempt strings that
    had already been reviewed.
    """
    ledger = Ledger.load(REPO_ROOT)
    baseline = Baseline.load(REPO_ROOT)
    stale = sorted(baseline.keys & set(ledger.entries))
    assert not stale, (
        f"{len(stale)} baseline key(s) already have a decision. Prune them:\n"
        "    python3 -m app.utils.copyreview --report\n"
        "(the review tool prunes as it goes; this means the baseline was "
        "rewritten by hand)"
    )


@pytest.mark.skip(reason=(
    "copy review gate is off (Ryan, 2026-09-18): unreviewed copy is not a priority right now. The tool still works: python3 -m app.utils.copyreview --report. Delete the skips to re-arm."
))
def test_no_decision_points_at_a_string_that_is_gone(extraction):
    """Pending WRITES must still have somewhere to write to.

    An orphaned approval is fine and is kept on purpose, so a string that comes
    back comes back approved. An orphaned replacement is not: it is an edit
    with no target, and it would sit in the queue forever.
    """
    ledger = Ledger.load(REPO_ROOT)
    live = {u.key for u in extraction.units}
    stranded = [
        e for e in ledger.entries.values() if e.needs_write and e.id not in live
    ]
    assert not stranded, (
        "Unwritten decision(s) whose string is no longer in the source:\n  "
        + "\n  ".join(f"{e.id} {e.text!r} ({e.verdict})" for e in stranded)
    )
