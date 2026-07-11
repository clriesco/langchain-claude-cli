"""CLI behavior invariants this library's design depends on (v0.3 D3).

Each test names the invariant and the design decision that consumes it.
They run against the LIVE CLI (nightly via contract.yml, or locally with
OAuth):  pytest tests/contract_tests -m contract

Costs are bounded: haiku + max_budget_usd per run.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

pytestmark = pytest.mark.contract

MODEL = "claude-haiku-4-5"
BUDGET = 0.10


def _opts(**kw: Any) -> ClaudeAgentOptions:
    base: dict[str, Any] = dict(
        model=MODEL, tools=[], max_turns=3, max_budget_usd=BUDGET
    )
    base.update(kw)
    return ClaudeAgentOptions(**base)


async def _run(prompt: Any, options: ClaudeAgentOptions):
    msgs, result = [], None
    async for msg in query(prompt=prompt, options=options):
        msgs.append(msg)
        if isinstance(msg, ResultMessage):
            result = msg
    return msgs, result


def _weather_tool(executed: list):
    @tool(
        "get_weather",
        "Get the current weather for a city.",
        {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
    async def get_weather(args: dict) -> dict:
        executed.append(args)
        return {"content": [{"type": "text", "text": f"25C in {args['city']}"}]}

    return get_weather


def _defer_hook():
    async def hook(input_data, tool_use_id, context):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "defer",
            }
        }

    return {"PreToolUse": [HookMatcher(matcher="mcp__lc__.*", hooks=[hook])]}


def _tool_options(executed: list, *, defer: bool, resume: str | None = None):
    server = create_sdk_mcp_server(
        name="lc", version="1.0.0", tools=[_weather_tool(executed)]
    )
    return _opts(
        mcp_servers={"lc": server},
        allowed_tools=["mcp__lc__get_weather"],
        hooks=_defer_hook() if defer else None,
        resume=resume,
    )


def test_defer_stops_run_without_executing():
    """Invariant S1/D3(v0.1): defer -> stop_reason='tool_deferred', tool NOT run."""
    executed: list = []

    async def main():
        return await _run(
            "Weather in Tokyo? Use get_weather.", _tool_options(executed, defer=True)
        )

    msgs, result = asyncio.run(main())
    assert result is not None and result.stop_reason == "tool_deferred"
    assert not executed
    tool_uses = [
        b
        for m in msgs
        if isinstance(m, AssistantMessage)
        for b in m.content
        if isinstance(b, ToolUseBlock)
    ]
    assert tool_uses and tool_uses[0].input.get("city")


def test_resume_refires_pending_tool():
    """Invariant S1b/D3.5(v0.1): resume re-fires the pending call at the MCP handler."""
    executed: list = []

    async def main():
        _, r1 = await _run(
            "Weather in Tokyo? Use get_weather.", _tool_options(executed, defer=True)
        )
        assert r1.stop_reason == "tool_deferred"
        delivered: list = []

        async def empty():
            return
            yield

        _msgs2, r2 = await _run(
            empty(), _tool_options(delivered, defer=False, resume=r1.session_id)
        )
        return delivered, r2

    delivered, r2 = asyncio.run(main())
    assert delivered, "pending tool was not re-fired on resume"
    assert r2 is not None and not r2.is_error


def test_parallel_tool_calls_all_deferred():
    """Invariant S2(v0.1): N tool_use blocks in one turn, all deferred."""
    executed: list = []

    async def main():
        return await _run(
            "Get the weather for BOTH Tokyo and Paris, one call per city, in parallel.",
            _tool_options(executed, defer=True),
        )

    msgs, _result = asyncio.run(main())
    tool_uses = [
        b
        for m in msgs
        if isinstance(m, AssistantMessage)
        for b in m.content
        if isinstance(b, ToolUseBlock)
    ]
    assert len(tool_uses) == 2 and not executed


@pytest.mark.xfail(
    strict=False,
    reason="Contract finding 2026-07-11: replay fidelity is race-dependent — "
    "the CLI generates a live reply to each historical user message and the "
    "model may prefer its own reply over the injected assistant turn. "
    "history_mode='replay' is documented as experimental.",
)
def test_assistant_replay_honored():
    """Invariant S3(v0.1)/D3(v0.2): fabricated assistant turns are honored."""

    async def entries():
        yield {
            "type": "user",
            "message": {"role": "user", "content": "Invent a codename."},
        }
        yield {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Your codename is ZANZIBAR-42."}],
            },
        }
        yield {
            "type": "user",
            "message": {"role": "user", "content": "My codename? Answer with it only."},
        }

    async def main():
        return await _run(entries(), _opts(max_turns=2))

    msgs, _ = asyncio.run(main())
    text = "".join(
        getattr(b, "text", "")
        for m in msgs
        if isinstance(m, AssistantMessage)
        for b in m.content
    )
    assert "ZANZIBAR" in text.upper()


def test_streaming_event_shapes():
    """Invariant S5(v0.1)/D7: partial messages expose text/thinking deltas."""

    async def main():
        kinds = set()
        async for msg in query(
            prompt="What is 12*12? Think briefly, then answer.",
            options=_opts(
                include_partial_messages=True,
                thinking={"type": "enabled", "budget_tokens": 2000},
            ),
        ):
            if isinstance(msg, StreamEvent):
                delta = msg.event.get("delta", {})
                if msg.event.get("type") == "content_block_delta":
                    kinds.add(delta.get("type"))
        return kinds

    kinds = asyncio.run(main())
    assert "text_delta" in kinds and "thinking_delta" in kinds


@pytest.mark.skipif(
    __import__("os").environ.get("CI") == "true",
    reason="subscription rate-limit events require OAuth auth (not API key)",
)
def test_rate_limit_event_emitted():
    """Invariant S6(v0.2)/4.1: RateLimitEvent flows in non-interactive mode."""

    async def main():
        events = []
        async for msg in query(prompt="Say OK", options=_opts(max_turns=1)):
            if isinstance(msg, RateLimitEvent):
                events.append(msg)
        return events

    events = asyncio.run(main())
    assert events and events[-1].rate_limit_info.status


@pytest.mark.skipif(
    __import__("os").environ.get("CI") == "true",
    reason="proving OAuth fallback requires an OAuth login to fall back to",
)
def test_empty_env_var_neutralizes_api_key():
    """Invariant S9(v0.2)/auth='oauth': env override '' forces OAuth."""
    import os

    fake = "sk-ant-api03-INVALID-contract-test-00000000000000000000000000000000000"
    os.environ["ANTHROPIC_API_KEY"] = fake
    try:

        async def main():
            return await _run(
                "Say OK", _opts(max_turns=1, env={"ANTHROPIC_API_KEY": ""})
            )

        _, result = asyncio.run(main())
        assert result is not None and not result.is_error
    finally:
        del os.environ["ANTHROPIC_API_KEY"]


def test_effort_levels_accepted():
    """Invariant (S1 bonus): SDK EffortLevel matches ChatAnthropic's five levels."""
    from typing import get_args

    from claude_agent_sdk import types as sdk_types

    levels = set(get_args(sdk_types.EffortLevel))
    assert {"low", "medium", "high", "xhigh", "max"} <= levels
