"""Core library flows against recorded cassettes — no CLI, no quota (D1).

Record/refresh:  RECORD_CASSETTES=1 pytest tests/cassette_tests -p no:cacheprovider
The live equivalents remain in tests/integration_tests (-m integration).
"""

from __future__ import annotations

import base64

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from langchain_claude_cli import ChatClaudeCli

MODEL = "claude-haiku-4-5"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    )


@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"25C sunny in {city}"


@pytest.fixture()
def llm(cassette) -> ChatClaudeCli:
    return ChatClaudeCli(model=MODEL)


def test_invoke_basic(llm):
    r = llm.invoke("Reply with exactly: PONG")
    assert "PONG" in _text(r.content)
    assert r.usage_metadata["output_tokens"] > 0
    assert r.response_metadata["session_id"]


def test_multiturn_resume(llm):
    h1 = HumanMessage(content="My favorite number is 77. Say OK.")
    a1 = llm.invoke([h1])
    r = llm.invoke([h1, a1, HumanMessage(content="My favorite number? Digits only.")])
    assert "77" in _text(r.content)
    assert r.response_metadata["session_id"] == a1.response_metadata["session_id"]


def test_tool_cycle(llm):
    lt = llm.bind_tools([get_weather])
    msgs = [HumanMessage(content="Weather in Tokyo? Use get_weather.")]
    r1 = lt.invoke(msgs)
    assert r1.tool_calls and r1.tool_calls[0]["name"] == "get_weather"
    tc = r1.tool_calls[0]
    msgs += [
        r1,
        ToolMessage(content=get_weather.invoke(tc["args"]), tool_call_id=tc["id"]),
    ]
    r2 = lt.invoke(msgs)
    assert not r2.tool_calls
    assert "25" in _text(r2.content)


def test_parallel_tool_calls(llm):
    lt = llm.bind_tools([get_weather])
    r = lt.invoke(
        "Get the weather for BOTH Tokyo and Paris, one call per city, in parallel."
    )
    assert len(r.tool_calls) == 2


class Answer(BaseModel):
    answer: str
    confidence: float


def test_structured_output(llm):
    result = llm.with_structured_output(Answer).invoke("Capital of France?")
    assert isinstance(result, Answer)
    assert "aris" in result.answer


def test_streaming(llm):
    chunks = list(llm.stream("Count from 1 to 5, digits separated by spaces."))
    text = "".join(c.content for c in chunks if isinstance(c.content, str))
    assert len(chunks) > 2
    assert "5" in text
    assert any(c.usage_metadata for c in chunks)


def test_stop_sequence_in_stream(llm):
    chunks = list(llm.stream("Repeat exactly: alpha ### beta", stop=["###"]))
    text = "".join(c.content for c in chunks if isinstance(c.content, str))
    assert "###" not in text and "beta" not in text


_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 60>>stream\nBT /F1 24 Tf 100 700 Td "
    b"(Secret word: XYLOPHONE-99) Tj ET\nendstream\nendobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF"
)


def test_pdf_document(llm):
    r = llm.invoke(
        [
            HumanMessage(
                content=[
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.b64encode(_PDF).decode(),
                        },
                    },
                    {"type": "text", "text": "Secret word in the PDF? Word only."},
                ]
            )
        ]
    )
    assert "XYLOPHONE" in _text(r.content).upper()
