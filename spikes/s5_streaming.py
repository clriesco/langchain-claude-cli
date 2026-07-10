"""S5 — Streaming fidelity: do StreamEvents carry text, thinking and tool-input deltas?

Run A: thinking enabled -> expect thinking_delta + text_delta events.
Run B: deferred tool bound -> expect content_block_start(tool_use) + input_json_delta.

Run: .venv/bin/python spikes/s5_streaming.py
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookContext,
    HookMatcher,
    StreamEvent,
    create_sdk_mcp_server,
    query,
    tool,
)

MODEL = "claude-haiku-4-5"


@tool(
    "get_weather",
    "Get the current weather for a city.",
    {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)
async def get_weather(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "never runs"}]}


async def defer_hook(input_data, tool_use_id, context: HookContext):
    return {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "defer"}
    }


async def collect(prompt: str, options: ClaudeAgentOptions) -> Counter:
    kinds: Counter = Counter()
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, StreamEvent):
            ev = msg.event
            etype = ev.get("type", "?")
            if etype == "content_block_delta":
                kinds[f"delta:{ev.get('delta', {}).get('type', '?')}"] += 1
            elif etype == "content_block_start":
                kinds[f"start:{ev.get('content_block', {}).get('type', '?')}"] += 1
            else:
                kinds[etype] += 1
    return kinds


async def main() -> None:
    print("== Run A: thinking enabled ==")
    a = await collect(
        "What is 127*389? Think it through carefully, then answer.",
        ClaudeAgentOptions(
            model=MODEL,
            tools=[],
            max_turns=1,
            include_partial_messages=True,
            thinking={"type": "enabled", "budget_tokens": 4000},
        ),
    )
    for k, v in sorted(a.items()):
        print(f"  {k}: {v}")

    print("\n== Run B: deferred tool ==")
    server = create_sdk_mcp_server(name="lc", version="1.0.0", tools=[get_weather])
    b = await collect(
        "What's the weather in Tokyo? Use get_weather.",
        ClaudeAgentOptions(
            model=MODEL,
            tools=[],
            max_turns=2,
            include_partial_messages=True,
            mcp_servers={"lc": server},
            allowed_tools=["mcp__lc__get_weather"],
            hooks={"PreToolUse": [HookMatcher(matcher="mcp__lc__.*", hooks=[defer_hook])]},
        ),
    )
    for k, v in sorted(b.items()):
        print(f"  {k}: {v}")

    ok_text = a.get("delta:text_delta", 0) > 1
    ok_think = a.get("delta:thinking_delta", 0) > 1
    ok_tool = b.get("start:tool_use", 0) >= 1 and b.get("delta:input_json_delta", 0) >= 1
    print(f"\n== S5: text={ok_text} thinking={ok_think} tool_input={ok_tool} ==")


asyncio.run(main())
