"""S1b — Resume leg: on resume the CLI re-fires the pending deferred tool call.

Hypothesis (from s1 phase-2 observation): the delivery mechanism for the tool
result is NOT a user tool_result message, but letting the re-fired call execute
against an MCP handler that returns the stored ToolMessage content.

Phase 1: same as s1 (defer, capture, session_id).
Phase 2: resume with a handler that returns the stored result and NO defer hook.
         Prompt: empty stream (the pending call should drive the turn).

Run: .venv/bin/python spikes/s1b_resume.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookContext,
    HookMatcher,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

MODEL = "claude-haiku-4-5"

STORED_RESULT: dict[str, str] = {}  # tool_use_id -> result content


def make_tool(deliver: bool):
    @tool(
        "get_weather",
        "Get the current weather for a city.",
        {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    )
    async def get_weather(args: dict[str, Any]) -> dict[str, Any]:
        if not deliver:
            return {"content": [{"type": "text", "text": "SHOULD NEVER RUN"}]}
        text = STORED_RESULT.get("result", "NO RESULT STORED")
        print(f"  [handler] delivering stored result: {text}")
        return {"content": [{"type": "text", "text": text}]}

    return get_weather


async def defer_hook(
    input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
) -> dict[str, Any]:
    print(f"  [hook] defer {input_data.get('tool_name')}")
    return {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "defer"}
    }


def make_options(deliver: bool, resume: str | None = None) -> ClaudeAgentOptions:
    server = create_sdk_mcp_server(name="lc", version="1.0.0", tools=[make_tool(deliver)])
    hooks = None
    if not deliver:
        hooks = {"PreToolUse": [HookMatcher(matcher="mcp__lc__.*", hooks=[defer_hook])]}
    return ClaudeAgentOptions(
        model=MODEL,
        tools=[],
        max_turns=3,
        mcp_servers={"lc": server},
        allowed_tools=["mcp__lc__get_weather"],
        hooks=hooks,
        resume=resume,
    )


async def run(prompt_arg: Any, options: ClaudeAgentOptions):
    result, texts = None, []
    async for msg in query(prompt=prompt_arg, options=options):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if hasattr(b, "text"):
                    texts.append(b.text)
        if isinstance(msg, ResultMessage):
            result = msg
    return result, "".join(texts)


async def main() -> None:
    print("== Phase 1: defer ==")
    r1, _ = await run(
        "What is the weather in Tokyo? Use the get_weather tool.", make_options(deliver=False)
    )
    assert r1 and r1.deferred_tool_use, "phase 1 defer failed"
    print(f"  deferred: {r1.deferred_tool_use.name}({r1.deferred_tool_use.input}) session={r1.session_id}")

    STORED_RESULT["result"] = "22°C, light rain in Tokyo"

    print("\n== Phase 2: resume, handler delivers ==")

    async def empty_stream():
        return
        yield  # pragma: no cover

    try:
        r2, text2 = await asyncio.wait_for(
            run(empty_stream(), make_options(deliver=True, resume=r1.session_id)),
            timeout=120,
        )
        print(f"  final: {text2[:200]}")
        print(f"  result: subtype={r2.subtype if r2 else '?'} is_error={r2.is_error if r2 else '?'} stop_reason={r2.stop_reason if r2 else '?'}")
        ok = r2 is not None and not r2.is_error and "22" in text2
        print(f"\n== S1b {'PASSED' if ok else 'FAILED'} ==")
    except TimeoutError:
        print("\n== S1b FAILED: empty-stream resume timed out (CLI waits for input) ==")


asyncio.run(main())
