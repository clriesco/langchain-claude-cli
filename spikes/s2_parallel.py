"""S2 — Multiple tool calls in one turn with defer.

deferred_tool_use is a singular field: if the model emits 2+ tool_use blocks,
which one is carried? Are all deferred? What does resume re-fire?

Run: .venv/bin/python spikes/s2_parallel.py
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
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

MODEL = "claude-haiku-4-5"
DEFERRED: list[str] = []
DELIVERED: list[str] = []


def make_tool(deliver: bool):
    @tool(
        "get_weather",
        "Get the current weather for a single city.",
        {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    )
    async def get_weather(args: dict[str, Any]) -> dict[str, Any]:
        DELIVERED.append(args["city"])
        print(f"  [handler] delivering for {args['city']}")
        return {"content": [{"type": "text", "text": f"20°C and sunny in {args['city']}"}]}

    return get_weather


async def defer_hook(
    input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
) -> dict[str, Any]:
    DEFERRED.append(str(input_data.get("tool_input", {}).get("city")))
    print(f"  [hook] defer #{len(DEFERRED)}: {input_data.get('tool_input')}")
    return {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "defer"}
    }


def make_options(deliver: bool, resume: str | None = None) -> ClaudeAgentOptions:
    server = create_sdk_mcp_server(name="lc", version="1.0.0", tools=[make_tool(deliver)])
    hooks = (
        None
        if deliver
        else {"PreToolUse": [HookMatcher(matcher="mcp__lc__.*", hooks=[defer_hook])]}
    )
    return ClaudeAgentOptions(
        model=MODEL,
        tools=[],
        max_turns=5,
        mcp_servers={"lc": server},
        allowed_tools=["mcp__lc__get_weather"],
        hooks=hooks,
        resume=resume,
    )


async def run(prompt_arg: Any, options: ClaudeAgentOptions):
    result, texts, tool_uses = None, [], []
    async for msg in query(prompt=prompt_arg, options=options):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if hasattr(b, "text"):
                    texts.append(b.text)
                if isinstance(b, ToolUseBlock):
                    tool_uses.append((b.id, b.name, b.input))
        if isinstance(msg, ResultMessage):
            result = msg
    return result, "".join(texts), tool_uses


async def main() -> None:
    print("== Phase 1: ask for TWO cities in one shot ==")
    r1, _, tool_uses = await run(
        "Get the weather for BOTH Tokyo and Paris. Call get_weather once per city, "
        "in parallel in a single response if you can.",
        make_options(deliver=False),
    )
    print(f"  tool_use blocks emitted: {len(tool_uses)}")
    for tid, name, inp in tool_uses:
        print(f"    {tid} {name} {inp}")
    print(f"  hook defer fired: {len(DEFERRED)}x {DEFERRED}")
    print(f"  deferred_tool_use (singular): {r1.deferred_tool_use}")
    print(f"  stop_reason={r1.stop_reason}")

    print("\n== Phase 2: resume with delivering handler ==")

    async def empty_stream():
        return
        yield

    r2, text2, _ = await asyncio.wait_for(
        run(empty_stream(), make_options(deliver=True, resume=r1.session_id)), timeout=180
    )
    print(f"  handler delivered for: {DELIVERED}")
    print(f"  final: {text2[:250]}")
    print(f"  stop_reason={r2.stop_reason if r2 else '?'} is_error={r2.is_error if r2 else '?'}")

    both = "Tokyo" in text2 and "Paris" in text2
    print(f"\n== S2 outcome: {len(tool_uses)} emitted, {len(DEFERRED)} deferred, "
          f"final covers both cities: {both} ==")


asyncio.run(main())
