"""
cli.py — the same picture the gate sees, outside pytest.

    python3 -m app.utils.copyreview --report
    python3 -m app.utils.copyreview --report --filter never-seen
    python3 -m app.utils.copyreview --baseline          # write the day-one backlog
    python3 -m app.utils.copyreview --diff              # what the pending decisions would do
    python3 -m app.utils.copyreview --apply             # write them

Report only, by default. `--apply` is the one flag that changes a file, and it
still goes through every guard in writeback.py.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from . import writeback
from .extract import extract
from .group import SCENARIOS
from .ledger import Baseline, Ledger, unreviewed

REPO_ROOT = Path(__file__).resolve().parents[3]


def _report(repo_root: Path, which: str | None) -> int:
    ex = extract(repo_root)
    led = Ledger.load(repo_root)
    base = Baseline.load(repo_root)
    matches = led.match(ex)
    by_key = {m.unit.key: m for m in matches}

    facing = ex.reviewable()
    pool = facing
    if which == "never-seen":
        pool = ex.never_seen()
    elif which == "em-dash":
        pool = ex.em_dashed()
    elif which in SCENARIOS:
        pool = [u for u in facing if u.scenario == which]

    open_items = [u for u in pool if by_key[u.key].status in ("new", "changed")]
    print(f"Copy review — {repo_root}")
    print(f"  user-facing strings   {len(facing):>5}   ({sum(u.occurrences for u in facing)} sites)")
    print(f"  decided               {len(led.entries):>5}")
    print(f"  unreachable by click  {len(ex.never_seen()):>5}")
    print(f"  containing an em dash {len(ex.em_dashed()):>5}")
    print(f"  in the gate baseline  {len(base.keys):>5}")

    statuses = Counter(by_key[u.key].status for u in facing)
    print("\n  status:", ", ".join(f"{k} {v}" for k, v in sorted(statuses.items())))

    changed = [m for m in matches if m.status == "changed" and m.unit.user_facing]
    if changed:
        print(f"\n  CHANGED SINCE APPROVAL ({len(changed)}) — approved copy was reworded:")
        for m in changed[:20]:
            print(f"    {m.unit.sites[0].file}:{m.unit.sites[0].line}")
            print(f"      was {m.was!r}")
            print(f"      now {m.unit.text!r}")

    pending = [e for e in led.entries.values() if e.needs_write]
    if pending:
        print(f"\n  pending write: {len(pending)} decision(s) not yet in the source")

    gate = unreviewed(matches, base)
    print(f"\n  the gate would fail on {len(gate)} string(s)")

    if which:
        print(f"\n  unreviewed in '{which}' ({len(open_items)}):")
        for u in open_items[:60]:
            site = u.sites[0]
            print(f"    {site.file}:{site.line:<6} [{u.scenario}] {u.text[:70]!r}")
        if len(open_items) > 60:
            print(f"    … and {len(open_items) - 60} more")
    return 0


def _write_baseline(repo_root: Path) -> int:
    ex = extract(repo_root)
    led = Ledger.load(repo_root)
    base = Baseline.load(repo_root)
    if base.keys:
        print(f"A baseline already exists with {len(base.keys)} key(s).")
        print("It is a ratchet: it only ever shrinks. Delete the file by hand if you")
        print("really mean to start it over.")
        return 1
    matches = led.match(ex)
    base.keys = {m.unit.key for m in matches if m.unit.user_facing and m.status in ("new", "changed")}
    base.save()
    print(f"Wrote {base.path} with {len(base.keys)} unreviewed string(s).")
    print("From here the gate fails on anything NEW. Remove keys as you review them.")
    return 0


def _write(repo_root: Path, *, apply: bool, confirm_bare: bool) -> int:
    ex = extract(repo_root)
    led = Ledger.load(repo_root)
    plan = writeback.build(ex, led, confirm_bare_removal=confirm_bare)

    for warning in plan.warnings:
        print("WARN  ", warning)
    for refusal in plan.refusals:
        print("REFUSE", refusal)
    if not plan.splices:
        print("Nothing to write.")
        return 1 if plan.refusals else 0

    print(f"\n{len(plan.splices)} splice(s) across {len(plan.by_file())} file(s)\n")
    print(writeback.diff(repo_root, plan))

    result = writeback.apply(
        repo_root, ex, plan, dry_run=not apply, ledger=led if apply else None
    )
    if result.refused:
        for refusal in result.refused:
            print("REFUSE", refusal)
        return 1
    if apply:
        print("Wrote:", ", ".join(result.files))
        print("Read the diff with `git diff` before committing.")
    else:
        print("Dry run. Nothing was written. Add --apply to write.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m app.utils.copyreview")
    parser.add_argument("--report", action="store_true", help="what is unreviewed")
    parser.add_argument(
        "--filter",
        dest="which",
        choices=("never-seen", "em-dash", *SCENARIOS),
        help="narrow the report",
    )
    parser.add_argument("--baseline", action="store_true", help="write the gate's day-one backlog")
    parser.add_argument("--diff", action="store_true", help="what the pending decisions would do")
    parser.add_argument("--apply", action="store_true", help="write the pending decisions")
    parser.add_argument(
        "--confirm-empty-literals",
        action="store_true",
        help="allow a 'remove' on a bare literal to empty it to ''",
    )
    parser.add_argument("--root", default=str(REPO_ROOT), help="repo root")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if args.baseline:
        return _write_baseline(root)
    if args.apply or args.diff:
        return _write(root, apply=args.apply, confirm_bare=args.confirm_empty_literals)
    return _report(root, args.which)


if __name__ == "__main__":
    sys.exit(main())
