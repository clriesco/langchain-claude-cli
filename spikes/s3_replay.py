"""S3 — Can stream-json input replay assistant messages (arbitrary history)?

Feeds a fabricated history: user msg + assistant msg (with a distinctive fact
we invented) + follow-up user question that can only be answered from the
fabricated assistant turn. If the model answers with the fact, replay works.

Run: .venv/bin/python spikes/s3_replay.py
"""

from __future__ import annotations

import asyncio

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

MODEL = "claude-haiku-4-5"


async def history_stream():
    yield {
        "type": "user",
        "message": {"role": "user", "content": "Invent a codename for my project."},
    }
    yield {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Your project codename is ZANZIBAR-42."}
            ],
        },
    }
    yield {
        "type": "user",
        "message": {
            "role": "user",
            "content": "What codename did you just give me? Answer with the codename only.",
        },
    }


async def main() -> None:
    options = ClaudeAgentOptions(model=MODEL, tools=[], max_turns=1)
    texts, result = [], None
    try:
        async for msg in query(prompt=history_stream(), options=options):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if hasattr(b, "text"):
                        texts.append(b.text)
            if isinstance(msg, ResultMessage):
                result = msg
    except Exception as e:
        print(f"EXCEPTION: {type(e).__name__}: {e}")
        print("\n== S3 FAILED (input rejected) ==")
        return

    final = "".join(texts)
    print(f"final: {final!r}")
    print(f"is_error={result.is_error if result else '?'} num_turns={result.num_turns if result else '?'}")
    ok = "ZANZIBAR" in final.upper()
    print(f"\n== S3 {'PASSED — assistant replay works' if ok else 'FAILED — history not honored'} ==")


asyncio.run(main())
