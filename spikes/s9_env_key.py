"""S9 (task 4.6) — Can options.env neutralize an inherited ANTHROPIC_API_KEY?

Uses an INVALID fake key so no real billing can occur:
  (a) fake key in os.environ, no override  -> if CLI uses the key, run FAILS (auth)
                                              => proves the leak
  (b) fake key in os.environ + env override ANTHROPIC_API_KEY="" -> if run WORKS,
      empty-string override forces OAuth => library-level guard is possible

Run: .venv/bin/python spikes/s9_env_key.py
"""

from __future__ import annotations

import asyncio
import os

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

FAKE = "sk-ant-api03-INVALID-spike-test-000000000000000000000000000000000000000000"


async def run(env_override: dict | None) -> tuple[bool, str]:
    opts = ClaudeAgentOptions(
        model="claude-haiku-4-5", tools=[], max_turns=1, env=env_override or {}
    )
    try:
        result = None
        async for msg in query(prompt="Reply with exactly: OK", options=opts):
            if isinstance(msg, ResultMessage):
                result = msg
        if result is None:
            return False, "no result"
        return (not result.is_error), f"subtype={result.subtype}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"


async def main() -> None:
    os.environ["ANTHROPIC_API_KEY"] = FAKE

    ok_a, detail_a = await run(None)
    print(f"(a) fake key inherited, no override -> ok={ok_a} ({detail_a})")

    ok_b, detail_b = await run({"ANTHROPIC_API_KEY": ""})
    print(f"(b) fake key + override ''          -> ok={ok_b} ({detail_b})")

    del os.environ["ANTHROPIC_API_KEY"]

    print()
    if not ok_a and ok_b:
        print("CONCLUSION: leak confirmed AND empty-string override neutralizes it — auth='oauth' guard is implementable via options.env")
    elif ok_a:
        print("CONCLUSION: CLI ignored the API key (no leak in this CLI version?) — re-check")
    else:
        print("CONCLUSION: override does NOT neutralize — guard needs another mechanism")


asyncio.run(main())
