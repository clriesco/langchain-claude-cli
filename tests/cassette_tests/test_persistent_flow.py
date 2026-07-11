"""Persistent-client flow against recorded client cassettes (v0.4 D2)."""

from __future__ import annotations

import time

from langchain_core.messages import HumanMessage

from langchain_claude_cli import ChatClaudeCli
from tests._cassettes import RECORDING

MODEL = "claude-haiku-4-5"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    )


def test_persistent_reuse_flow(cassette):
    llm = ChatClaudeCli(model=MODEL, persistent=True)
    try:
        h1 = HumanMessage(content="My lucky number is 13. Say OK.")
        a1 = llm.invoke([h1])

        deadline = time.time() + 10
        while len(llm._pool) < 1 and time.time() < deadline:
            time.sleep(0.05)
        assert len(llm._pool) >= 1, "pool never warmed"

        r = llm.invoke(
            [h1, a1, HumanMessage(content="Lucky number plus 1? Digits only.")]
        )
        assert "14" in _text(r.content)
        assert r.response_metadata["session_id"] == a1.response_metadata["session_id"]
        # The second turn was served by the pooled client, not query():
        query_exchanges = len(cassette.exchanges) if RECORDING else cassette.index
        assert query_exchanges == 1, "expected exactly one stateless query() exchange"
        assert len(cassette.clients) == 1
    finally:
        llm._pool.close()
