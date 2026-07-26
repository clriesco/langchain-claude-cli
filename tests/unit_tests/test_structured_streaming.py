"""Structured output over the streaming path (1.0.0) — no CLI required.

`with_structured_output()` reads `additional_kwargs["structured_output"]`.
The invoke path sets it from the ResultMessage; the streaming path used to
drop it, leaving the parser to fall back to reading text that a structured
run never emits (its content is thinking + tool_use), so `.stream()` died
with a JSONDecodeError on an empty string.
"""

import claude_agent_sdk
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage
from claude_agent_sdk.types import TextBlock
from pydantic import BaseModel

from langchain_claude_cli import ChatClaudeCli

PAYLOAD = {"setup": "Why did the cat sit on the computer?", "punchline": "The mouse."}


class Joke(BaseModel):
    """Joke to tell user."""

    setup: str
    punchline: str


def _structured_query(structured):
    """query() double: a structured run answers with no text content."""

    def fake_query(*, prompt, options):
        async def gen():
            if not isinstance(prompt, str):
                async for _ in prompt:
                    pass
            yield AssistantMessage(content=[TextBlock(text="")], model="haiku")
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="11111111-1111-1111-1111-111111111111",
                structured_output=structured,
            )

        return gen()

    return fake_query


def _aggregate(chunks):
    agg = chunks[0]
    for chunk in chunks[1:]:
        agg = agg + chunk
    return agg


def test_streaming_carries_structured_output(monkeypatch):
    monkeypatch.setattr(claude_agent_sdk, "query", _structured_query(PAYLOAD))
    llm = ChatClaudeCli(model="haiku")
    agg = _aggregate(list(llm.stream("tell me a joke")))
    assert agg.additional_kwargs["structured_output"] == PAYLOAD


def test_streaming_without_structured_output_adds_nothing(monkeypatch):
    """A plain run must not grow a spurious key."""
    monkeypatch.setattr(claude_agent_sdk, "query", _structured_query(None))
    llm = ChatClaudeCli(model="haiku")
    agg = _aggregate(list(llm.stream("hola")))
    assert "structured_output" not in agg.additional_kwargs


@pytest.mark.parametrize("schema", [Joke, dict(Joke.model_json_schema())])
def test_with_structured_output_streams_the_parsed_object(monkeypatch, schema):
    monkeypatch.setattr(claude_agent_sdk, "query", _structured_query(PAYLOAD))
    llm = ChatClaudeCli(model="haiku")
    out = list(llm.with_structured_output(schema).stream("tell me a joke"))
    assert out[-1] == (Joke(**PAYLOAD) if schema is Joke else PAYLOAD)
