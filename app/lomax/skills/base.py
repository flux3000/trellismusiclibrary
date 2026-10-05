"""The Skill definition, the Subject a run is about, and the exception a skill raises
when there is nothing for it to do. A skill is data plus three small callables."""
from dataclasses import dataclass, field
from typing import Callable, Optional


class NothingToDo(Exception):
    """Raised by a skill's gather(): no model call is made and the run finishes empty.
    The message becomes the result's `thinking`."""


@dataclass
class Ctx:
    """What a run carries into gather(): the level, the question, the search budget."""
    level: str = "research"
    question: str = ""
    max_searches: int = 4
    user_id: Optional[int] = None


@dataclass
class Subject:
    type: str                       # recording | folder | artist | venue
    id: Optional[int] = None
    key: Optional[str] = None       # folder path for "folder"
    obj: object = None              # the ORM row (recording, artist, venue); None for a folder
    current: dict = field(default_factory=dict)   # what the page knows: always set for a folder
    scratch: dict = field(default_factory=dict)   # gather() leaves notes for normalize() here


@dataclass(frozen=True)
class Skill:
    key: str                        # resolution | recording | artist | venue
    subject_types: tuple
    instructions: str               # this skill's section of the system prompt
    submit_tool: dict               # output schema: one tool, called once
    gather: Callable                # (subject, ctx) -> list[EvidenceSection]
    normalize: Callable             # (raw tool input, subject) -> (result dict, proposal dicts)
    apply_auto: Optional[Callable] = None   # (subject, result) -> dict of what was applied; Artist History only
    base_tokens: int = 4000         # typical input tokens before any search, for estimates
    label: str = ""
