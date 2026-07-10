"""S4 — Are document (PDF) content blocks accepted in stream-json input?

Builds a tiny valid PDF containing a distinctive word, sends it as an
Anthropic-style document block, and asks the model to read it.

Run: .venv/bin/python spikes/s4_document.py
"""

from __future__ import annotations

import asyncio
import base64

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

MODEL = "claude-haiku-4-5"

# Minimal one-page PDF with the text "XYLOPHONE-99"
PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 60>>stream
BT /F1 24 Tf 100 700 Td (Secret word: XYLOPHONE-99) Tj ET
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
trailer<</Root 1 0 R>>
%%EOF"""


async def doc_stream():
    yield {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(PDF).decode(),
                    },
                },
                {
                    "type": "text",
                    "text": "What is the secret word in this PDF? Answer with the word only.",
                },
            ],
        },
    }


async def main() -> None:
    options = ClaudeAgentOptions(model=MODEL, tools=[], max_turns=1)
    texts, result = [], None
    try:
        async for msg in query(prompt=doc_stream(), options=options):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if hasattr(b, "text"):
                        texts.append(b.text)
            if isinstance(msg, ResultMessage):
                result = msg
    except Exception as e:
        print(f"EXCEPTION: {type(e).__name__}: {e}")
        print("\n== S4 FAILED (document block rejected) ==")
        return

    final = "".join(texts)
    print(f"final: {final[:300]!r}")
    print(f"is_error={result.is_error if result else '?'}")
    ok = "XYLOPHONE" in final.upper()
    print(f"\n== S4 {'PASSED — document blocks work' if ok else 'FAILED/DEGRADED'} ==")


asyncio.run(main())
