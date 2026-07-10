"""S1 — Round-trip defer: MCP tool + PreToolUse->defer, then resume with tool_result.

Validates the core tool-calling mechanism (design D3):
1. Register a LangChain-style tool as an in-process MCP server.
2. PreToolUse hook returns permissionDecision="defer".
3. Assert: the tool handler is NOT executed and ResultMessage.deferred_tool_use
   carries {id, name, input}.
4. Resume the session sending a tool_result block; assert the final answer
   incorporates the tool output.

Also prints the EffortLevel values supported by the SDK.

Run: .venv/bin/python spikes/s1_defer.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
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

EXECUTED: list[dict] = []


@tool(
    "get_weather",
    "Get the current weather for a city.",
    {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)
async def get_weather(args: dict[str, Any]) -> dict[str, Any]:
    EXECUTED.append(args)  # must stay empty in classic mode
    return {"content": [{"type": "text", "text": "SHOULD NEVER RUN"}]}


async def defer_hook(
    input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
) -> dict[str, Any]:
    print(f"  [hook] PreToolUse fired: {input_data.get('tool_name')} -> defer")
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "defer",
        }
    }


def base_options() -> ClaudeAgentOptions:
    server = create_sdk_mcp_server(name="lc", version="1.0.0", tools=[get_weather])
    return ClaudeAgentOptions(
        model=MODEL,
        tools=[],  # no built-in tools: pure-LLM mode
        max_turns=3,
        mcp_servers={"lc": server},
        allowed_tools=["mcp__lc__get_weather"],
        hooks={"PreToolUse": [HookMatcher(matcher="mcp__lc__.*", hooks=[defer_hook])]},
    )


async def run(prompt_arg: Any, options: ClaudeAgentOptions) -> tuple[ResultMessage | None, list]:
    result, msgs = None, []
    async for msg in query(prompt=prompt_arg, options=options):
        msgs.append(msg)
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                print(f"  [assistant] {type(block).__name__}: {getattr(block, 'text', getattr(block, 'name', ''))[:80]}")
        if isinstance(msg, ResultMessage):
            result = msg
    return result, msgs


async def main() -> None:
    print("== EffortLevel values ==")
    import claude_agent_sdk.types as t

    print(" ", getattr(t, "EffortLevel", "NOT FOUND"))

    print("\n== Phase 1: invoke with deferred tool ==")
    opts = base_options()
    result, _ = await run("What is the weather in Tokyo? Use the get_weather tool.", opts)

    assert result is not None, "no ResultMessage"
    print(f"  subtype={result.subtype} stop_reason={result.stop_reason} is_error={result.is_error}")
    print(f"  deferred_tool_use={result.deferred_tool_use}")
    print(f"  handler executed: {EXECUTED or 'NO (correct)'}")
    print(f"  session_id={result.session_id}")

    if not result.deferred_tool_use:
        print("\n!! S1 FAILED at phase 1: no deferred_tool_use — dumping result")
        print(json.dumps({k: str(v) for k, v in vars(result).items()}, indent=2))
        return

    dtu = result.deferred_tool_use
    assert not EXECUTED, "tool handler ran despite defer!"

    print("\n== Phase 2: resume with tool_result ==")

    async def tool_result_stream():
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": dtu.id,
                        "content": "22°C, light rain in Tokyo",
                    }
                ],
            },
        }

    opts2 = replace(base_options(), resume=result.session_id)
    result2, msgs2 = await run(tool_result_stream(), opts2)

    final_text = "".join(
        getattr(b, "text", "")
        for m in msgs2
        if isinstance(m, AssistantMessage)
        for b in m.content
    )
    print(f"\n  final answer: {final_text[:200]}")
    print(f"  result2: subtype={result2.subtype if result2 else '?'} is_error={result2.is_error if result2 else '?'}")

    ok = result2 is not None and not result2.is_error and "22" in final_text
    print(f"\n== S1 {'PASSED' if ok else 'FAILED'} ==")


asyncio.run(main())
