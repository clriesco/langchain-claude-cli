"""S7 — Does stream-json input accept Files API blocks (source type "file")?

We can't create a real file_id without an API key, but the failure MODE is
diagnostic: input-format rejection => unsupported shape; an API-side
"file not found"-style error => the shape passed through to the API.

Run: .venv/bin/python spikes/s7_files_api.py
"""

from __future__ import annotations

import asyncio

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query


async def file_stream():
    yield {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {"type": "file", "file_id": "file-011CNha8iCJcU1wXNR6q4V8w"},
                },
                {"type": "text", "text": "What does this file contain?"},
            ],
        },
    }


async def main() -> None:
    try:
        result, texts = None, []
        async for msg in query(
            prompt=file_stream(),
            options=ClaudeAgentOptions(model="claude-haiku-4-5", tools=[], max_turns=1),
        ):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if hasattr(b, "text"):
                        texts.append(b.text)
            if isinstance(msg, ResultMessage):
                result = msg
        print("completed. is_error:", result.is_error if result else "?")
        print("subtype:", result.subtype if result else "?")
        print("errors:", getattr(result, "errors", None))
        print("text:", " ".join(texts)[:200])
    except Exception as e:
        print(f"EXCEPTION: {type(e).__name__}: {str(e)[:300]}")


asyncio.run(main())
