"""Unit tests for ChatClaudeCli that do not require the CLI."""

import warnings

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

import langchain_claude_cli._compat as compat
from langchain_claude_cli import ChatClaudeCli, ClaudeCliCompatWarning
from langchain_claude_cli._convert import sdk_blocks_to_lc
from langchain_claude_cli.chat_models import (
    _apply_stop_and_max_tokens,
    _lc_tool_to_anthropic,
)


@pytest.fixture(autouse=True)
def _reset_warned():
    compat._warned.clear()


# ── Signature parity (level C no-ops) ────────────────────────


def test_constructor_accepts_chatanthropic_params():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        llm = ChatClaudeCli(
            model="claude-haiku-4-5",
            temperature=0.2,
            top_k=40,
            top_p=0.9,
            max_tokens=1024,
            max_retries=3,
            stop_sequences=["END"],
            default_headers={"x": "y"},
            anthropic_api_key="ignored",
            thinking={"type": "enabled", "budget_tokens": 1000},
            effort="high",
        )
    assert llm.model == "claude-haiku-4-5"
    noop_warnings = [
        w for w in caught if issubclass(w.category, ClaudeCliCompatWarning)
    ]
    assert {("temperature" in str(w.message)) for w in noop_warnings}
    assert len(noop_warnings) >= 4  # temperature, top_k, top_p, default_headers


def test_aliases_match_chatanthropic():
    llm = ChatClaudeCli(
        model_name="m", max_tokens_to_sample=99, timeout=12.5, stop=["a"]
    )
    assert llm.model == "m"
    assert llm.max_tokens == 99
    assert llm.default_request_timeout == 12.5
    assert llm.stop_sequences == ["a"]


def test_noop_warning_emitted_once_per_process():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ChatClaudeCli(temperature=0.5)
        ChatClaudeCli(temperature=0.7)
    temp_warnings = [w for w in caught if "temperature" in str(w.message)]
    assert len(temp_warnings) == 1


# ── Tool conversion ──────────────────────────────────────────


def test_lc_tool_to_anthropic_from_pydantic():
    class GetWeather(BaseModel):
        """Get the weather."""

        city: str

    schema = _lc_tool_to_anthropic(GetWeather)
    assert schema["name"] == "GetWeather"
    assert "city" in schema["input_schema"]["properties"]


def test_lc_tool_to_anthropic_passthrough_and_openai():
    anthropic = {"name": "f", "description": "d", "input_schema": {"type": "object"}}
    assert _lc_tool_to_anthropic(anthropic) == anthropic
    openai = {
        "type": "function",
        "function": {"name": "g", "description": "e", "parameters": {"type": "object"}},
    }
    out = _lc_tool_to_anthropic(openai)
    assert out["name"] == "g"
    assert out["input_schema"] == {"type": "object"}


def test_bind_tools_routes_server_tools_to_builtins():
    llm = ChatClaudeCli()
    bound = llm.bind_tools(
        [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}]
    )
    assert bound.kwargs["server_builtin_tools"] == ["WebSearch"]
    assert bound.kwargs["tools"] == []


def test_bind_tools_warns_on_strict():
    llm = ChatClaudeCli()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        llm.bind_tools([{"name": "f", "input_schema": {}}], strict=True)
    assert any("strict" in str(w.message) for w in caught)


# ── Level B workarounds ──────────────────────────────────────


def test_stop_sequence_truncation():
    content, reason = _apply_stop_and_max_tokens("hello STOP world", ["STOP"], None)
    assert content == "hello "
    assert reason == "stop_sequence"


def test_max_tokens_truncation():
    content, reason = _apply_stop_and_max_tokens("x" * 100, None, 10)
    assert len(content) == 40  # 10 tokens * ~4 chars
    assert reason == "max_tokens"


def test_no_truncation_when_unset():
    content, reason = _apply_stop_and_max_tokens("hello", None, None)
    assert content == "hello"
    assert reason is None


def test_get_num_tokens_heuristic():
    llm = ChatClaudeCli()
    n = llm.get_num_tokens_from_messages([HumanMessage(content="word " * 100)])
    assert 50 < n < 250


# ── Delivered-call filtering (agent-loop regression) ─────────


class _Block:
    pass


def _tool_use(id_: str, name: str, input_: dict):
    class ToolUseBlock(_Block):
        def __init__(self):
            self.id, self.name, self.input = id_, name, input_

    return ToolUseBlock()


def _assistant(blocks):
    class FakeAssistantMessage:
        def __init__(self):
            self.content = blocks

    return FakeAssistantMessage()


def test_delivered_calls_not_reported_again():
    """A resumed run re-emits delivered tool_use blocks; they must not surface."""
    msgs = [_assistant([_tool_use("t1", "mcp__lc__w", {"city": "T"})])]
    _, tool_calls = sdk_blocks_to_lc(
        msgs, deferred=True, delivered_ids={"t1"}, delivered_keys=set()
    )
    assert tool_calls == []


def test_non_deferred_run_reports_no_tool_calls():
    msgs = [_assistant([_tool_use("t1", "mcp__lc__w", {"city": "T"})])]
    _, tool_calls = sdk_blocks_to_lc(msgs, deferred=False)
    assert tool_calls == []


def test_mixed_delivered_and_new_calls():
    msgs = [
        _assistant(
            [
                _tool_use("t1", "mcp__lc__w", {"city": "T"}),
                _tool_use("t2", "mcp__lc__calc", {"e": "1+1"}),
            ]
        )
    ]
    _, tool_calls = sdk_blocks_to_lc(
        msgs, deferred=True, delivered_ids={"t1"}, delivered_keys=set()
    )
    assert [tc["id"] for tc in tool_calls] == ["t2"]


# ── Structured output plumbing ───────────────────────────────


def test_with_structured_output_parses_additional_kwargs():
    class Answer(BaseModel):
        answer: str

    llm = ChatClaudeCli()
    chain = llm.with_structured_output(Answer)
    # Grab the parser (last step) and feed it a message directly.
    parser = chain.steps[-1]
    msg = AIMessage(
        content="", additional_kwargs={"structured_output": {"answer": "x"}}
    )
    assert parser.invoke(msg) == Answer(answer="x")


def test_with_structured_output_include_raw_captures_errors():
    class Answer(BaseModel):
        answer: str

    llm = ChatClaudeCli()
    chain = llm.with_structured_output(Answer, include_raw=True)
    parser = chain.steps[-1]
    bad = AIMessage(content="not json at all")
    out = parser.invoke(bad)
    assert out["parsed"] is None
    assert out["parsing_error"] is not None
    assert out["raw"] is bad
