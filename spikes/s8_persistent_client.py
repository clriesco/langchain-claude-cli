"""S8 — ClaudeSDKClient reuse: latency vs query()+resume, interrupt, set_model.

Run: .venv/bin/python spikes/s8_persistent_client.py
"""

from __future__ import annotations

import asyncio
import time

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    query,
)

MODEL = "claude-haiku-4-5"
OPTS = dict(model=MODEL, tools=[], max_turns=1)


async def stateless_pair() -> tuple[float, float]:
    t0 = time.time()
    session = None
    async for msg in query(prompt="Say A", options=ClaudeAgentOptions(**OPTS)):
        if isinstance(msg, ResultMessage):
            session = msg.session_id
    t1 = time.time()
    async for msg in query(
        prompt="Say B", options=ClaudeAgentOptions(**OPTS, resume=session)
    ):
        if isinstance(msg, ResultMessage):
            pass
    t2 = time.time()
    return t1 - t0, t2 - t1


async def persistent_triplet() -> tuple[float, float, float, str]:
    client = ClaudeSDKClient(ClaudeAgentOptions(**OPTS))
    await client.connect()
    times = []
    text_last = ""
    try:
        for prompt in ("Say A", "Say B", "Say C"):
            t0 = time.time()
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if hasattr(b, "text"):
                            text_last = b.text
            times.append(time.time() - t0)
    finally:
        await client.disconnect()
    return times[0], times[1], times[2], text_last


async def interrupt_and_setmodel() -> str:
    client = ClaudeSDKClient(ClaudeAgentOptions(**OPTS))
    await client.connect()
    try:
        # Launch a long generation, interrupt it shortly after
        await client.query("Count slowly from 1 to 500, one number per line.")

        async def consume() -> int:
            n = 0
            async for msg in client.receive_response():
                n += 1
            return n

        task = asyncio.create_task(consume())
        await asyncio.sleep(3)
        await client.interrupt()
        try:
            await asyncio.wait_for(task, timeout=20)
            interrupted = "interrupt OK (stream ended)"
        except TimeoutError:
            task.cancel()
            interrupted = "interrupt TIMEOUT (stream did not end)"

        # set_model + follow-up query on the same client
        await client.set_model("claude-sonnet-4-5")
        await client.query("Which model are you? One short line.")
        text = ""
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if hasattr(b, "text"):
                        text += b.text
        return f"{interrupted} | post-interrupt+set_model reply: {text[:80]!r}"
    finally:
        await client.disconnect()


async def main() -> None:
    s1, s2 = await stateless_pair()
    print(f"stateless : first={s1:.1f}s  resume={s2:.1f}s")
    p1, p2, p3, _ = await persistent_triplet()
    print(f"persistent: first={p1:.1f}s  reuse2={p2:.1f}s  reuse3={p3:.1f}s")
    print(f"reuse speedup vs resume: {s2 / ((p2 + p3) / 2):.1f}x")
    print(await interrupt_and_setmodel())


asyncio.run(main())
