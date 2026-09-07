"""
tests/test_ai_assist_v2.py — the 2026-09-07 AI Assist pass.

Covers the things that are cheap to get wrong and expensive to notice: the
search budget, the prompt-cache marker, the info-file cap, the previous-run
recap, and the DB-grounding block. All of these are about TOKENS, and a
regression in any of them costs real money on someone else's Anthropic bill
without changing a single visible pixel — which is exactly the kind of bug
that survives a manual look at the app.

No network. A fake client captures the kwargs handed to messages.create, so
these assert what we actually SEND, not what we believe we send.
"""

import sys
import types

import pytest

from app.utils import ai_assist, performer_research
from app.utils.ai_assist import MAX_SEARCHES, MAX_SEARCHES_WITH_QUESTION


class _FakeBlock:
    type = "tool_use"

    def __init__(self, name, data):
        self.name = name
        self.input = data


class _FakeResponse:
    stop_reason = "tool_use"

    def __init__(self, name, data):
        self.content = [_FakeBlock(name, data)]
        self.usage = types.SimpleNamespace(
            input_tokens=1000, output_tokens=100,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
            server_tool_use=types.SimpleNamespace(web_search_requests=2))


class _FakeClient:
    """Records the call instead of making it."""
    last = {}

    def __init__(self, *a, **kw):
        self.messages = self

    def create(self, **kwargs):
        _FakeClient.last = kwargs
        for tool in kwargs.get("tools", []):
            if tool.get("name") == "submit_lineup":
                return _FakeResponse("submit_lineup",
                                     {"thinking": "t", "members": [{"name": "X", "confidence": "low"}]})
            if tool.get("name") == "submit_dossier":
                return _FakeResponse("submit_dossier", {"thinking": "t", "biography": "b"})
        return _FakeResponse("submit_analysis", {"thinking": "t", "proposals": []})


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Swap the SDK for a recorder in both research modules."""
    fake = types.SimpleNamespace(
        Anthropic=_FakeClient,
        AuthenticationError=type("AuthenticationError", (Exception,), {}),
        APITimeoutError=type("APITimeoutError", (Exception,), {}),
        APIError=type("APIError", (Exception,), {}),
    )
    monkeypatch.setattr(ai_assist, "anthropic", fake, raising=False)
    monkeypatch.setattr(ai_assist, "_HAS_SDK", True)
    monkeypatch.setattr(performer_research, "anthropic", fake, raising=False)
    monkeypatch.setattr(performer_research, "_HAS_SDK", True)
    _FakeClient.last = {}
    return _FakeClient


def _user_text(call):
    """All the text blocks of the single user turn, joined."""
    content = call["messages"][0]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content)


def _max_uses(call):
    for tool in call["tools"]:
        if tool.get("name") == "web_search":
            return tool["max_uses"]
    raise AssertionError("no web_search tool in the call")


# ── Search budget ───────────────────────────────────────────────────────────
# Search is ~95% of the token bill and worse than linear, so the default budget
# is the single highest-leverage number in this feature.

def test_a_plain_run_uses_the_smaller_search_budget(fake_anthropic):
    ai_assist.run_ai_assist("/x/Folder", {"artist": "A"}, "key", "claude-sonnet-5")
    assert _max_uses(fake_anthropic.last) == MAX_SEARCHES
    assert MAX_SEARCHES < MAX_SEARCHES_WITH_QUESTION   # or the question buys nothing


def test_a_question_earns_the_bigger_search_budget(fake_anthropic):
    ai_assist.run_ai_assist("/x/Folder", {"artist": "A"}, "key", "claude-sonnet-5",
                            question="Is this really the Cellar Door show?")
    assert _max_uses(fake_anthropic.last) == MAX_SEARCHES_WITH_QUESTION
    assert "Is this really the Cellar Door show?" in _user_text(fake_anthropic.last)


def test_a_blank_question_is_not_a_question(fake_anthropic):
    # An untouched input field posts "" — that must not silently buy the bigger
    # budget on every single run.
    ai_assist.run_ai_assist("/x/F", {"artist": "A"}, "key", "m", question="   ")
    assert _max_uses(fake_anthropic.last) == MAX_SEARCHES


# ── Prompt caching ──────────────────────────────────────────────────────────

def test_the_system_prompt_is_marked_cacheable(fake_anthropic):
    ai_assist.run_ai_assist("/x/F", {"artist": "A"}, "key", "m")
    system = fake_anthropic.last["system"]
    assert isinstance(system, list), "system must be blocks, or cache_control has nowhere to go"
    assert system[-1]["cache_control"] == {"type": "ephemeral"}


# ── Info-file cap ───────────────────────────────────────────────────────────

def test_a_normal_info_file_is_sent_whole(fake_anthropic):
    body = "Set I\nBig Mon\nRawhide\n" * 20
    ai_assist.run_ai_assist("/x/F", {"info_file_content": body}, "key", "m")
    text = _user_text(fake_anthropic.last)
    assert "Rawhide" in text
    assert "TRUNCATED" not in text


def test_an_enormous_info_file_is_capped_and_says_so(fake_anthropic):
    body = "x" * (ai_assist._INFO_FILE_CHAR_CAP + 5000)
    ai_assist.run_ai_assist("/x/F", {"info_file_content": body}, "key", "m")
    text = _user_text(fake_anthropic.last)
    assert len(text) < len(body)
    # Silently truncating would let the model confidently report a short setlist.
    assert "TRUNCATED" in text


# ── Previous-run recap ──────────────────────────────────────────────────────

def test_no_prior_means_no_recap(fake_anthropic):
    ai_assist.run_ai_assist("/x/F", {"artist": "A"}, "key", "m", prior=None)
    assert "PREVIOUS RUN" not in _user_text(fake_anthropic.last)


def test_a_prior_run_comes_back_labelled_as_not_corroboration(fake_anthropic):
    prior = {"proposals": [{"field": "date", "proposed": "1978-12-31", "confidence": "high"}],
             "verify_items": ["Track 4 title uncertain"]}
    ai_assist.run_ai_assist("/x/F", {"artist": "A"}, "key", "m", prior=prior)
    text = _user_text(fake_anthropic.last)
    assert "1978-12-31" in text
    assert "Track 4 title uncertain" in text
    # The whole risk of feeding a previous run back is the model treating its
    # own past output as a second source. The warning is the feature.
    assert "corroborates nothing" in text


def test_an_empty_prior_blob_adds_nothing(fake_anthropic):
    # A run that proposed nothing is a real, common outcome — it must not
    # produce a recap header with no content under it.
    ai_assist.run_ai_assist("/x/F", {"artist": "A"}, "key", "m",
                            prior={"proposals": [], "verify_items": []})
    assert "PREVIOUS RUN" not in _user_text(fake_anthropic.last)


# ── lineage became proposable ───────────────────────────────────────────────

def test_lineage_can_be_proposed():
    # It was sent to the model from the start but missing from this list, so the
    # model could read a wrong lineage and had no way to say so.
    assert "lineage" in ai_assist._PROPOSAL_FIELDS


# ── Performer research: grounding, modes ────────────────────────────────────

def test_context_block_is_empty_when_nothing_is_known():
    assert performer_research._context_block({}) == ""
    assert performer_research._context_block(None) == ""


def test_context_block_carries_the_db_facts():
    block = performer_research._context_block({
        "aliases": ["The Dead"], "mb_area": "Palo Alto", "mb_begin": "1965",
        "genre": "Rock", "members": ["Jerry Garcia", "Phil Lesh"],
    })
    assert "The Dead" in block
    assert "Palo Alto" in block
    assert "Jerry Garcia" in block
    assert "ground truth" in block


def test_bio_mode_and_lineup_mode_send_different_tools(fake_anthropic):
    performer_research.run_performer_research("Act", "", "key", "m", mode="bio")
    bio_tools = {t.get("name") for t in fake_anthropic.last["tools"]}
    assert "submit_dossier" in bio_tools

    performer_research.run_performer_research("Act", "", "key", "m", mode="lineup")
    lineup_call = fake_anthropic.last
    assert "submit_lineup" in {t.get("name") for t in lineup_call["tools"]}
    assert "LINEUP" in lineup_call["system"][-1]["text"]
    assert lineup_call["system"][-1]["cache_control"] == {"type": "ephemeral"}


def test_lineup_mode_returns_members(fake_anthropic):
    got = performer_research.run_performer_research("Act", "", "key", "m", mode="lineup")
    assert got["mode"] == "lineup"
    assert got["members"] == [{"name": "X", "confidence": "low"}]
    assert got["usage"]["total_tokens"] == 1100
    assert "cost_cents" not in got["usage"]


def test_an_unknown_mode_is_refused(fake_anthropic):
    with pytest.raises(ai_assist.AiAssistError):
        performer_research.run_performer_research("Act", "", "key", "m", mode="astrology")


def test_the_existing_bio_is_only_sent_to_the_bio_pass(fake_anthropic):
    # Lineup research has no use for the prose, and sending it is pure tokens.
    performer_research.run_performer_research("Act", "An old draft bio.", "key", "m", mode="lineup")
    assert "An old draft bio" not in _user_text(fake_anthropic.last)
    performer_research.run_performer_research("Act", "An old draft bio.", "key", "m", mode="bio")
    assert "An old draft bio" in _user_text(fake_anthropic.last)
