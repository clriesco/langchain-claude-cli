"""Unit tests for _convert (no CLI required)."""

from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from langchain_claude_cli._convert import (
    convert_lc_messages,
    flatten_to_single_user,
    result_to_response_metadata,
    sdk_blocks_to_lc,
    usage_to_usage_metadata,
)

# ── SDK block stand-ins (same class names as claude_agent_sdk.types) ──


@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = "sig"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class FakeAssistantMessage:
    content: list = field(default_factory=list)


@dataclass
class FakeResult:
    stop_reason: str | None = "end_turn"
    session_id: str = "sess-1"
    total_cost_usd: float | None = 0.01
    num_turns: int = 1
    duration_ms: int = 900
    usage: dict[str, Any] | None = None


# ── LangChain -> CLI ─────────────────────────────────────────


def test_system_and_human_text():
    out = convert_lc_messages(
        [SystemMessage(content="be brief"), HumanMessage(content="hi")]
    )
    assert out.system == "be brief"
    assert out.entries == [
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        }
    ]


def test_multiple_system_messages_concatenate():
    out = convert_lc_messages([SystemMessage(content="a"), SystemMessage(content="b")])
    assert out.system == "a\n\nb"


def test_image_base64_and_url():
    msg = HumanMessage(
        content=[
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}},
            {"type": "image_url", "image_url": {"url": "https://x.test/a.jpg"}},
        ]
    )
    blocks = convert_lc_messages([msg]).entries[0]["message"]["content"]
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAA="},
    }
    assert blocks[2] == {
        "type": "image",
        "source": {"type": "url", "url": "https://x.test/a.jpg"},
    }


def test_cache_control_is_stripped():
    msg = HumanMessage(
        content=[
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "A"},
                "cache_control": {"type": "ephemeral"},
            }
        ]
    )
    block = convert_lc_messages([msg]).entries[0]["message"]["content"][0]
    assert "cache_control" not in block


def test_ai_message_with_tool_calls_becomes_namespaced_tool_use():
    msg = AIMessage(
        content="checking",
        tool_calls=[{"name": "get_weather", "args": {"city": "Tokyo"}, "id": "t1"}],
    )
    entry = convert_lc_messages([msg]).entries[0]
    assert entry["type"] == "assistant"
    blocks = entry["message"]["content"]
    assert blocks[0] == {"type": "text", "text": "checking"}
    assert blocks[1] == {
        "type": "tool_use",
        "id": "t1",
        "name": "mcp__lc__get_weather",
        "input": {"city": "Tokyo"},
    }


def test_tool_messages_fill_delivery_map_not_entries():
    msgs = [
        HumanMessage(content="weather?"),
        AIMessage(
            content="",
            tool_calls=[{"name": "get_weather", "args": {"city": "T"}, "id": "t1"}],
        ),
        ToolMessage(content="22°C", tool_call_id="t1"),
    ]
    out = convert_lc_messages(msgs)
    assert out.tool_results == {"t1": "22°C"}
    assert out.tool_results_by_name == {"get_weather": "22°C"}
    assert out.has_pending_tool_results
    # ToolMessage must NOT appear as a stream entry (spike S1)
    assert all(e["type"] != "user" or "tool_result" not in str(e) for e in out.entries)


def test_flatten_preserves_images_and_labels_roles():
    entries = convert_lc_messages(
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": "what is this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,A"},
                    },
                ]
            ),
            AIMessage(content="a cat"),
            HumanMessage(content="what color?"),
        ]
    ).entries
    flat = flatten_to_single_user(entries)
    assert flat["type"] == "user"
    blocks = flat["message"]["content"]
    labels = [b["text"] for b in blocks if b["type"] == "text"]
    assert any("[User]: what is this" in t for t in labels)
    assert any("[Assistant]: a cat" in t for t in labels)
    assert sum(1 for b in blocks if b["type"] == "image") == 1


# ── CLI -> LangChain ─────────────────────────────────────────


def test_sdk_blocks_plain_text_collapses_to_string():
    msgs = [FakeAssistantMessage(content=[TextBlock("hello "), TextBlock("world")])]
    content, tool_calls = sdk_blocks_to_lc(msgs, deferred=False)
    assert content == "hello world"
    assert tool_calls == []


def test_sdk_blocks_deferred_drops_post_defer_text():
    msgs = [
        FakeAssistantMessage(
            content=[
                TextBlock("let me check"),
                ToolUseBlock("t1", "mcp__lc__get_weather", {"city": "Tokyo"}),
                TextBlock("the tool encountered an issue"),  # defer reaction: drop
            ]
        )
    ]
    content, tool_calls = sdk_blocks_to_lc(msgs, deferred=True)
    assert content == "let me check"
    assert tool_calls == [
        {
            "name": "get_weather",
            "args": {"city": "Tokyo"},
            "id": "t1",
            "type": "tool_call",
        }
    ]


def test_sdk_blocks_multiple_tool_calls_all_captured():
    msgs = [
        FakeAssistantMessage(
            content=[
                ToolUseBlock("t1", "mcp__lc__w", {"city": "Tokyo"}),
                ToolUseBlock("t2", "mcp__lc__w", {"city": "Paris"}),
            ]
        )
    ]
    _, tool_calls = sdk_blocks_to_lc(msgs, deferred=True)
    assert [tc["id"] for tc in tool_calls] == ["t1", "t2"]


def test_sdk_blocks_thinking_kept_as_block():
    msgs = [FakeAssistantMessage(content=[ThinkingBlock("hmm"), TextBlock("42")])]
    content, _ = sdk_blocks_to_lc(msgs, deferred=False)
    assert content[0] == {"type": "thinking", "thinking": "hmm", "signature": "sig"}
    assert content[1] == {"type": "text", "text": "42"}


def test_usage_metadata_with_cache_tokens():
    meta = usage_to_usage_metadata(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 20,
        }
    )
    assert meta == {
        "input_tokens": 130,
        "output_tokens": 5,
        "total_tokens": 135,
        "input_token_details": {"cache_read": 100, "cache_creation": 20},
    }


def test_usage_metadata_none():
    assert usage_to_usage_metadata(None) is None


def test_response_metadata():
    meta = result_to_response_metadata(FakeResult(), model="claude-haiku-4-5")
    assert meta["stop_reason"] == "end_turn"
    assert meta["session_id"] == "sess-1"
    assert meta["total_cost_usd"] == 0.01
    assert meta["model_name"] == "claude-haiku-4-5"
