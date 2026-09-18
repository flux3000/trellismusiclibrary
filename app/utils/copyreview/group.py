"""
group.py — the scenario a string belongs to, from its syntactic position.

This is the part that earns the tool its keep. Reading the app tells you about
the copy on the happy path; it tells you nothing about the text in a `catch`
block, behind an empty-state guard, or in an API error you would have to break
the server to see. Those are the strings a user meets on their worst day, and
they are the ones nobody has read.

Position is proof, not a guess. A string inside `catch_clause` is on an error
path whatever it says.
"""

from __future__ import annotations

import re

from .jsparse import Context, display_text

ERROR = "error"
EMPTY = "empty"
MODAL = "modal"
AFFORDANCE = "affordance"
UI = "ui"

SCENARIOS = (ERROR, EMPTY, MODAL, AFFORDANCE, UI)

_MODAL_CALLS = {"alert", "confirm", "prompt", "window.alert", "window.confirm", "window.prompt"}

# An emptiness guard: a bare negation, or a count compared to zero.
_EMPTY_GUARD = re.compile(
    r"^\s*!\s*[\w.$\[\]]+\s*$"
    r"|\.length\s*===?\s*0"
    r"|\.length\s*<\s*1"
    r"|!\s*[\w.$\[\]]*\.length"
    r"|^\s*!\s*[\w.$\[\]]+\??\.\w+"
)

EM_DASH = "—"


def scenario_of(kind: str, ctx: Context) -> str:
    if kind == "py_error":
        return ERROR
    if ctx.in_catch:
        return ERROR
    if ctx.call_name in _MODAL_CALLS:
        return MODAL
    if kind == "html_attr":
        return AFFORDANCE
    if ctx.guard_text and _EMPTY_GUARD.search(ctx.guard_text):
        return EMPTY
    return UI


def never_seen(kind: str, ctx: Context, scenario: str) -> bool:
    """Is this string unreachable by clicking around the running app?

    Three ways in, and all three are structural: a catch block needs a real
    failure, an empty-state guard needs an empty library, and an API error
    needs the request to go wrong. Together these are the strings that a
    click-through review cannot cover.
    """
    if kind == "py_error":
        return True
    if ctx.in_catch:
        return True
    return scenario == EMPTY


def has_em_dash(text: str) -> bool:
    return EM_DASH in display_text(text)
