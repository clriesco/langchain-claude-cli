"""Round-trip tests for the cassette serializer (task 1.1)."""

import json

from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
)
from claude_agent_sdk.types import RateLimitInfo, TextBlock, ThinkingBlock, ToolUseBlock

from tests._cassettes import from_jsonable, to_jsonable


def _roundtrip(obj):
    return from_jsonable(json.loads(json.dumps(to_jsonable(obj))))


def test_assistant_message_with_blocks():
    msg = AssistantMessage(
        content=[
            ThinkingBlock(thinking="hmm", signature="sig"),
            TextBlock(text="hello"),
            ToolUseBlock(id="t1", name="mcp__lc__w", input={"city": "Tokyo"}),
        ],
        model="claude-haiku-4-5",
    )
    out = _roundtrip(msg)
    assert isinstance(out, AssistantMessage)
    assert isinstance(out.content[0], ThinkingBlock)
    assert isinstance(out.content[1], TextBlock)
    assert isinstance(out.content[2], ToolUseBlock)
    assert out.content[2].input == {"city": "Tokyo"}


def test_result_message():
    msg = ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        stop_reason="end_turn",
        total_cost_usd=0.01,
        usage={"input_tokens": 5, "output_tokens": 3},
    )
    out = _roundtrip(msg)
    assert isinstance(out, ResultMessage)
    assert out.stop_reason == "end_turn"
    assert out.usage == {"input_tokens": 5, "output_tokens": 3}


def test_stream_event_raw_dict():
    ev = StreamEvent(
        uuid="u1",
        session_id="s1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hi"},
        },
    )
    out = _roundtrip(ev)
    assert isinstance(out, StreamEvent)
    assert out.event["delta"]["text"] == "hi"


def test_rate_limit_event():
    ev = RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="allowed", resets_at=123, rate_limit_type="five_hour"
        ),
        uuid="u1",
        session_id="s1",
    )
    out = _roundtrip(ev)
    assert isinstance(out, RateLimitEvent)
    assert out.rate_limit_info.status == "allowed"
