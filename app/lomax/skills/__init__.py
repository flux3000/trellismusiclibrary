"""Skill registry. Adding Lomax to a new entity means one new file here and one line below."""
from app.lomax.skills.base import Ctx, NothingToDo, Skill, Subject  # noqa: F401
from app.lomax.skills import artist, recording, venue

SKILLS = {s.key: s for s in (recording.SKILL, artist.SKILL, venue.SKILL)}
