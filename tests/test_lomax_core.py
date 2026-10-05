"""
tests/test_lomax_core.py -- the shared Lomax machinery: the one SDK call, search budget, prompt
caching, levels, usage, prose cleaning, error mapping and the prompt text every skill shares.

No network. A fake client records the kwargs handed to messages.create, so these assert what is
actually SENT. The fake is also what keeps every other test_lomax_* module off the network.
"""
import types

import pytest

import app.lomax as lomax
from app.lomax import core, prompts
from app.lomax.core import MAX_SEARCHES, MAX_SEARCHES_WITH_QUESTION
from app.lomax.skills import SKILLS


class _Block:
    type = "tool_use"

    def __init__(self, name, data):
        self.name, self.input = name, data


class _Response:
    stop_reason = "tool_use"

    def __init__(self, name, data):
        self.content = [_Block(name, data)]
        self.usage = types.SimpleNamespace(
            input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0,
            server_tool_use=types.SimpleNamespace(web_search_requests=2))


DEFAULTS = {
    "submit_recording_research": {"thinking": "t", "proposals": [], "tracks": []},
    "submit_artist_history": {"thinking": "t", "biography": "A bio.", "members": [], "resources": []},
    "submit_venue_history": {"thinking": "t", "history": "h", "proposals": []},
}


class FakeClient:
    """Records the call instead of making it. `canned` overrides a skill's reply by tool name."""
    calls = []
    canned = {}
    raises = None

    def __init__(self, *a, **kw):
        self.messages = self

    def create(self, **kwargs):
        FakeClient.calls.append(kwargs)
        if FakeClient.raises:
            raise FakeClient.raises
        name = next(t["name"] for t in kwargs["tools"] if t["name"].startswith("submit_"))
        return _Response(name, FakeClient.canned.get(name, DEFAULTS[name]))


@pytest.fixture
def fake(monkeypatch):
    sdk = types.SimpleNamespace(
        Anthropic=FakeClient,
        AuthenticationError=type("AuthenticationError", (Exception,), {}),
        APITimeoutError=type("APITimeoutError", (Exception,), {}),
        APIError=type("APIError", (Exception,), {}))
    monkeypatch.setattr(core, "anthropic", sdk, raising=False)
    monkeypatch.setattr(core, "_HAS_SDK", True)
    FakeClient.calls, FakeClient.canned, FakeClient.raises = [], {}, None
    FakeClient.sdk = sdk
    return FakeClient


pytestmark = pytest.mark.usefixtures("fake")


def user_text(call):
    return call["messages"][0]["content"]


def web_tool(call):
    return next((t for t in call["tools"] if t.get("name") == "web_search"), None)


def run_skill(skill, subject_type, subject_id, **kw):
    return lomax.run_now(skill, subject_type, subject_id=subject_id, api_key="k", **kw)


@pytest.fixture
def rec_id(app, seeded_ids):
    return seeded_ids["recording_id"]


# ── levels and search budget ─────────────────────────────────────────────────

def test_read_level_sends_no_web_tool_and_forces_the_submit_tool(fake, rec_id):
    run = run_skill("recording", "recording", rec_id, level="study")
    assert run.status == "done"
    call = fake.calls[-1]
    assert web_tool(call) is None
    assert call["tool_choice"] == {"type": "tool", "name": "submit_recording_research"}


def test_research_level_sends_web_search_with_the_small_budget(fake, rec_id):
    run_skill("recording", "recording", rec_id)
    assert web_tool(fake.calls[-1])["max_uses"] == MAX_SEARCHES
    assert MAX_SEARCHES < MAX_SEARCHES_WITH_QUESTION   # or a question buys nothing


def test_a_question_earns_the_bigger_budget_and_reaches_the_prompt(fake, rec_id):
    run_skill("recording", "recording", rec_id, question="Is this really the Sprague Hall show?")
    assert web_tool(fake.calls[-1])["max_uses"] == MAX_SEARCHES_WITH_QUESTION
    assert "The archivist asks: Is this really the Sprague Hall show?" in user_text(fake.calls[-1])


def test_a_blank_question_is_not_a_question(fake, rec_id):
    run_skill("recording", "recording", rec_id, question="   ")
    assert web_tool(fake.calls[-1])["max_uses"] == MAX_SEARCHES
    assert "The archivist asks" not in user_text(fake.calls[-1])


def test_the_prompt_states_the_level(fake, rec_id):
    run_skill("recording", "recording", rec_id, level="study")
    assert "Level: Study" in user_text(fake.calls[-1])
    run_skill("recording", "recording", rec_id)
    assert "Level: Research" in user_text(fake.calls[-1])


# ── caching and one model default ────────────────────────────────────────────

def test_base_is_the_same_cached_block_for_every_skill(fake, app, seeded_ids):
    run_skill("recording", "recording", seeded_ids["recording_id"])
    run_skill("artist", "artist", seeded_ids["artist_id"])
    a, b = fake.calls[-2]["system"], fake.calls[-1]["system"]
    assert a[0] == b[0]
    assert a[0]["text"] == prompts.BASE
    assert a[0]["cache_control"] == {"type": "ephemeral"}
    assert a[1]["text"] != b[1]["text"]                # the skill's own section differs
    assert b[-1]["cache_control"] == {"type": "ephemeral"}


def test_one_model_default_is_used_everywhere(fake, app, seeded_ids):
    from app.models.user import User
    from app.utils.prefs import set_pref
    run_skill("recording", "recording", seeded_ids["recording_id"])
    assert fake.calls[-1]["model"] == core.MODEL_FALLBACK
    uid = User.query.first().id
    set_pref(uid, "ai_model", "claude-test-model")
    for skill, st, sid in (("artist", "artist", seeded_ids["artist_id"]),
                           ("recording", "recording", seeded_ids["recording_id"])):
        lomax.run_now(skill, st, subject_id=sid, api_key="k", user_id=uid)
        assert fake.calls[-1]["model"] == "claude-test-model"


# ── usage and estimates ──────────────────────────────────────────────────────

def test_usage_is_tokens_and_searches_never_currency(fake, rec_id):
    run = run_skill("recording", "recording", rec_id)
    usage = lomax.get_run(run.id)["usage"]
    assert usage["total_tokens"] == 1100 and usage["web_search_requests"] == 2
    assert not any("cent" in k or "cost" in k or "usd" in k for k in usage)
    assert core.usage_summary(None) is None            # not measured is not zero


def test_cache_counts_stay_separate_from_plain_input():
    got = core.usage_summary(types.SimpleNamespace(
        input_tokens=800, output_tokens=200, cache_read_input_tokens=60000,
        cache_creation_input_tokens=0, server_tool_use=None))
    assert got["cache_read_input_tokens"] == 60000 and got["input_tokens"] == 800
    assert got["web_search_requests"] == 0


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_estimates_are_ranges_per_skill_and_level(skill):
    read = lomax.estimate(skill, "study")
    research = lomax.estimate(skill, "research")
    assert read["low_tokens"] < read["high_tokens"] and read["max_searches"] == 0
    assert research["max_searches"] == MAX_SEARCHES
    assert research["high_tokens"] > read["high_tokens"]
    assert lomax.estimate(skill, "research", True)["high_tokens"] > research["high_tokens"]


# ── extraction and prose ─────────────────────────────────────────────────────

def test_prose_is_cleaned_of_citation_markup(fake, app, seeded_ids):
    fake.canned["submit_artist_history"] = {
        "thinking": "Found it.<cite index=\"1\"> </cite>", "biography": "Formed in 1956 [1]. Toured widely [2,3] .",
        "members": []}
    run = run_skill("artist", "artist", seeded_ids["artist_id"])
    result = lomax.get_run(run.id)["result"]
    assert result["biography"] == "Formed in 1956. Toured widely."
    assert "cite" not in result["thinking"]


def test_a_reply_with_no_tool_call_becomes_the_thinking_text(fake, rec_id, monkeypatch):
    class Text:
        type, text = "text", "I could not decide."
    monkeypatch.setattr(fake, "create", lambda self, **kw: types.SimpleNamespace(
        content=[Text()], usage=None, stop_reason="end_turn"))
    run = run_skill("recording", "recording", rec_id)
    assert run.status == "done"
    assert lomax.get_run(run.id)["result"]["thinking"] == "I could not decide."


# ── errors ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("exc, text", [("AuthenticationError", "rejected the API key"),
                                       ("APITimeoutError", "timed out"),
                                       ("APIError", "Anthropic API error")])
def test_sdk_errors_become_a_readable_error_on_the_run(fake, rec_id, exc, text):
    fake.raises = getattr(fake.sdk, exc)("boom")
    run = run_skill("recording", "recording", rec_id)
    assert run.status == "error" and text in run.error and run.finished_at is not None


def test_no_api_key_is_an_error_not_a_crash(fake, rec_id):
    run = lomax.run_now("recording", "recording", subject_id=rec_id, api_key=None)
    assert run.status == "error" and "No Anthropic API key" in run.error
    assert fake.calls == []


# ── the info file ────────────────────────────────────────────────────────────

def _set_info(app, rec_id, text):
    from app.extensions import db
    from app.models.recording import Recording
    db.session.get(Recording, rec_id).info_file_content = text
    db.session.commit()


def test_a_normal_info_file_is_sent_whole(fake, app, rec_id):
    _set_info(app, rec_id, "Set I\nBig Mon\nRawhide\n" * 20)
    run_skill("recording", "recording", rec_id)
    text = user_text(fake.calls[-1])
    assert "Rawhide" in text and "TRUNCATED" not in text


def test_an_enormous_info_file_is_capped_at_20000_and_says_so(fake, app, rec_id):
    from app.lomax.evidence import INFO_FILE_CHAR_CAP
    assert INFO_FILE_CHAR_CAP == 20_000
    _set_info(app, rec_id, "x" * (INFO_FILE_CHAR_CAP + 5000))
    run_skill("recording", "recording", rec_id)
    text = user_text(fake.calls[-1])
    assert len(text) < INFO_FILE_CHAR_CAP + 4000
    assert "TRUNCATED" in text


# ── the shared prompt text ───────────────────────────────────────────────────

def test_state_covers_us_canada_and_australia_only():
    assert "State covers US states, Canadian provinces and Australian states only" in prompts.BASE
    assert "US only" not in prompts.BASE


def test_event_is_defined_as_the_spec_says():
    assert ("Event is a collection of performances comprising a festival or other single "
            "ticketed-or-free event, never a tour, residency or billing note") in prompts.BASE
    assert "Stage is the stage within an event or venue" in prompts.BASE


def test_evidence_order_and_the_rules_are_stated_in_base():
    b = prompts.BASE
    order = [b.index(h) for h in ('"Known already"', '"Reference matches"', '"Read from the files"',
                                  '"Trusted sources"', "open web")]
    assert order == sorted(order)
    flat = b.replace("\\\n", " ").replace("\n", " ")
    for phrase in ("Suggest, never assert", "2 or more independent sources", "Zero proposals is a good result",
                   "must also be a structured proposal", "A previous run is you, not a second source",
                   "Proposals the archivist rejected are listed as rejected", "answer it first"):
        assert phrase in flat, phrase


def test_always_research_the_setlist_exists_only_in_recording_research():
    for key, sk in SKILLS.items():
        assert "ALWAYS" not in sk.instructions, key
    assert "ALWAYS" not in prompts.BASE
    assert "track listing" in SKILLS["recording"].instructions
    for key in ("venue",):
        assert "setlist" not in SKILLS[key].instructions.replace("Do no setlist or track work", "").lower(), key


def test_no_em_dashes_in_any_prompt_text():
    texts = [prompts.BASE] + [s.instructions + s.submit_tool["description"] for s in SKILLS.values()]
    assert not any("—" in t for t in texts)


def test_the_always_empty_aliases_line_is_gone(fake, app, seeded_ids):
    run_skill("artist", "artist", seeded_ids["artist_id"])
    assert "aliases" not in user_text(fake.calls[-1]).lower()
    assert "Also known as" not in user_text(fake.calls[-1])
