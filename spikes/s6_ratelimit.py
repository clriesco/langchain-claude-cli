"""S6 — Does the CLI emit RateLimitEvent in non-interactive (SDK) mode?

Runs a simple query and inventories every message type seen in the stream.

Run: .venv/bin/python spikes/s6_ratelimit.py
"""

from __future__ import annotations

import asyncio
from collections import Counter

from claude_agent_sdk import ClaudeAgentOptions, RateLimitEvent, query


async def main() -> None:
    kinds: Counter = Counter()
    rate_events = []
    async for msg in query(
        prompt="Reply with exactly: OK",
        options=ClaudeAgentOptions(model="claude-haiku-4-5", tools=[], max_turns=1),
    ):
        kinds[type(msg).__name__] += 1
        if isinstance(msg, RateLimitEvent):
            rate_events.append(msg)

    print("message types seen:", dict(kinds))
    if rate_events:
        ev = rate_events[-1]
        print("RateLimitEvent:", ev)
        print("info:", getattr(ev, "rate_limit_info", None) or vars(ev))
    else:
        print("NO RateLimitEvent emitted in this run")


asyncio.run(main())
