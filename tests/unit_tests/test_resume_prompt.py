"""A resumed turn must never ship an empty prompt stream.

Claude Code accepted an empty prompt on resume through 2.1.234 and re-fired the
pending tool call. From 2.1.235 it exits non-zero with an EMPTY stderr, which
surfaces as an opaque `ProcessError` and orphans the in-flight control request.
Measured against 2.1.241: every `bind_tools` cycle on a resumed session died at
its second turn. The nightly contract suite caught it; these tests pin it.
"""

from __future__ import annotations

import claude_agent_sdk
import pytest
from claude_agent_sdk import ResultMessage
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from langchain_claude_cli import ChatClaudeCli
from langchain_claude_cli._convert import convert_lc_messages
from langchain_claude_cli._runner import _RESUME_CONTINUATION
from langchain_claude_cli._sessions import Resolution


@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"25C in {city}"


def _resolution(**kw):
    kw.setdefault("strategy", "resume")
    kw.setdefault("session_id", "4f0c2b1e-0000-4000-8000-000000000001")
    kw.setdefault("suffix", [])
    return Resolution(**kw)


# ── the prompt builder ───────────────────────────────────────


def test_resume_with_only_tool_results_gets_a_trigger():
    """The tool results rode the MCP handlers, leaving the prompt empty."""
    llm = ChatClaudeCli(model="claude-haiku-4-5")
    converted = convert_lc_messages([ToolMessage(content="25C", tool_call_id="t1")])
    assert converted.entries == [], "precondition: the suffix carries no entries"
    assert converted.tool_results, "precondition: the results ride the handlers"

    entries = llm._build_prompt_entries(_resolution(), converted)
    assert len(entries) == 1
    assert entries[0]["type"] == "user"
    assert entries[0]["message"]["content"][0]["text"] == _RESUME_CONTINUATION


def test_resume_with_a_real_message_is_untouched():
    """Only the degenerate empty case changes."""
    llm = ChatClaudeCli(model="claude-haiku-4-5")
    converted = convert_lc_messages([HumanMessage(content="and in Paris?")])
    entries = llm._build_prompt_entries(_resolution(), converted)
    assert len(entries) == 1
    assert entries[0]["message"]["content"][0]["text"] == "and in Paris?"


# ── end to end through the runner ────────────────────────────


def _recording_query(sent: list):
    """query() double that records the prompt entries it was handed."""

    def fake_query(*, prompt, options):
        async def gen():
            if isinstance(prompt, str):
                sent.append(prompt)
            else:
                async for entry in prompt:
                    sent.append(entry)
            yield ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=8,
                is_error=False,
                num_turns=1,
                session_id="4f0c2b1e-0000-4000-8000-000000000001",
                total_cost_usd=0.001,
                usage={"input_tokens": 5, "output_tokens": 2},
            )

        return gen()

    return fake_query


def test_tool_cycle_second_turn_sends_a_non_empty_prompt(monkeypatch):
    """The measured break: turn 2 of a bind_tools cycle used to send nothing."""
    sent: list = []
    monkeypatch.setattr(claude_agent_sdk, "query", _recording_query(sent))

    llm = ChatClaudeCli(model="claude-haiku-4-5", max_retries=0)
    bound = llm.bind_tools([get_weather])

    history = [
        HumanMessage(content="Weather in Tokyo?"),
        AIMessage(
            content="",
            tool_calls=[{"name": "get_weather", "args": {"city": "Tokyo"}, "id": "t1"}],
        ),
        ToolMessage(content="25C in Tokyo", tool_call_id="t1"),
    ]
    # Pin the session so this turn resolves as a resume, as it would after the
    # first turn registered its mapping.
    llm._session_cache.register(history[:2], "4f0c2b1e-0000-4000-8000-000000000001")

    sent.clear()
    bound.invoke(history)
    assert sent, "the CLI was handed an empty prompt stream — 2.1.235+ exits 1"
    texts = [
        block.get("text", "")
        for entry in sent
        for block in entry.get("message", {}).get("content", [])
        if isinstance(block, dict)
    ]
    assert any(t.strip() for t in texts), f"prompt carried no text: {sent}"


@pytest.mark.parametrize("streaming", [False, True], ids=["invoke", "stream"])
def test_neither_path_ever_sends_an_empty_prompt(monkeypatch, streaming):
    """`_build_prompt_entries` is shared, so both paths must be covered."""
    sent: list = []
    monkeypatch.setattr(claude_agent_sdk, "query", _recording_query(sent))

    llm = ChatClaudeCli(model="claude-haiku-4-5", max_retries=0)
    history = [
        HumanMessage(content="Weather in Tokyo?"),
        AIMessage(content="25C."),
        ToolMessage(content="25C in Tokyo", tool_call_id="t1"),
    ]
    llm._session_cache.register(history[:2], "4f0c2b1e-0000-4000-8000-000000000001")

    sent.clear()
    if streaming:
        list(llm.stream(history))
    else:
        llm.invoke(history)
    assert sent, "empty prompt stream reached the CLI"
