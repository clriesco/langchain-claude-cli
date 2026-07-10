"""Message conversion between LangChain and the Claude Agent SDK / CLI.

LangChain -> CLI: messages become stream-json input entries (user/assistant
dicts with Anthropic-style content blocks). Tool results are NOT sent as
messages (spike S1: the CLI ignores user tool_result blocks); they are
collected into a delivery map consumed by the MCP handlers on session resume.

CLI -> LangChain: SDK content blocks become ChatAnthropic-style message content
(anthropic-native block dicts) plus tool_calls, usage_metadata and
response_metadata.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import UsageMetadata

MCP_SERVER_NAME = "lc"
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"


def strip_tool_namespace(name: str) -> str:
    """mcp__lc__get_weather -> get_weather (non-lc names pass through)."""
    return name[len(MCP_TOOL_PREFIX) :] if name.startswith(MCP_TOOL_PREFIX) else name


def add_tool_namespace(name: str) -> str:
    return name if name.startswith(MCP_TOOL_PREFIX) else f"{MCP_TOOL_PREFIX}{name}"


def canonical_args(args: Any) -> str:
    """Canonical JSON for tool args, used as delivery-map key."""
    import json

    return json.dumps(args, sort_keys=True, default=str)


# ── LangChain -> CLI ─────────────────────────────────────────


FileResolver = Callable[[str], dict | None]
"""file_id -> materialized base64 content block, or None if unavailable."""


def _human_item_to_block(
    item: str | dict, file_resolver: FileResolver | None = None
) -> dict | None:
    """Convert one HumanMessage content item to an Anthropic content block.

    Returns None for blocks that must be dropped (unresolvable file_id).
    """
    if isinstance(item, str):
        return {"type": "text", "text": item}
    kind = item.get("type", "")
    if kind == "text":
        return {"type": "text", "text": item.get("text", "")}
    if kind == "image_url":
        img = item.get("image_url", {})
        url = img if isinstance(img, str) else img.get("url", "")
        if url.startswith("data:"):
            header, b64data = url.split(",", 1)
            media_type = header.split(":", 1)[1].split(";", 1)[0]
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64data,
                },
            }
        return {"type": "image", "source": {"type": "url", "url": url}}
    if kind in ("image", "document"):
        source = item.get("source", {})
        if isinstance(source, dict) and source.get("type") == "file":
            # Files API block: a file_id belongs to an API account and cannot
            # resolve under the CLI's OAuth session (spike S7) — materialize
            # via the resolver (Anthropic API) or drop with a warning.
            resolved = (
                file_resolver(source.get("file_id", "")) if file_resolver else None
            )
            if resolved is None:
                from langchain_claude_cli._compat import warn_once

                warn_once(
                    "files_api",
                    "A Files API block (file_id) could not be materialized — "
                    "the CLI's OAuth session cannot access API file storage. "
                    "Provide ANTHROPIC_API_KEY (used ONLY to download the file, "
                    "never passed to the CLI) or inline the content as base64. "
                    "The block was omitted.",
                )
                return None
            return resolved
        return {k: v for k, v in item.items() if k != "cache_control"}
    if (
        kind == "file"  # langchain-core v1 standard file block
        and item.get("source_type") == "base64"
        and item.get("mime_type") == "application/pdf"
    ):
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": item.get("data", ""),
            },
        }
    return {"type": "text", "text": str(item)}


def _human_content_to_blocks(
    content: str | list, file_resolver: FileResolver | None = None
) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks = [_human_item_to_block(item, file_resolver) for item in content]
    return [b for b in blocks if b is not None]


def _ai_message_to_blocks(msg: AIMessage) -> list[dict]:
    """AIMessage -> assistant content blocks (text + tool_use), for replay/flatten."""
    blocks: list[dict] = []
    if isinstance(msg.content, str):
        if msg.content:
            blocks.append({"type": "text", "text": msg.content})
    else:
        for item in msg.content:
            if isinstance(item, str):
                blocks.append({"type": "text", "text": item})
            elif isinstance(item, dict) and item.get("type") == "text":
                blocks.append({"type": "text", "text": item.get("text", "")})
            # thinking/tool_use dict blocks from a prior turn: skipped — tool_use
            # is re-added below from msg.tool_calls; replayed thinking is noise.
    blocks.extend(
        {
            "type": "tool_use",
            "id": tc["id"],
            "name": add_tool_namespace(tc["name"]),
            "input": tc["args"],
        }
        for tc in msg.tool_calls
    )
    return blocks


@dataclass
class ConvertedHistory:
    """LangChain history converted for CLI consumption."""

    system: str | None = None
    # stream-json input entries, in order: {"type": "user"|"assistant", "message": {...}}
    entries: list[dict] = field(default_factory=list)
    # tool_call_id -> stringified result, from ToolMessages (delivery map)
    tool_results: dict[str, str] = field(default_factory=dict)
    # (un-namespaced tool name, canonical-json args) -> result
    tool_results_by_key: dict[tuple[str, str], str] = field(default_factory=dict)
    # tool name (un-namespaced) -> result, fallback when args don't match
    tool_results_by_name: dict[str, str] = field(default_factory=dict)
    # tool_call_id -> (un-namespaced name, canonical args) for every AI tool call
    tool_call_meta: dict[str, tuple[str, str]] = field(default_factory=dict)

    def restrict_results(self, ids: set[str]) -> ConvertedHistory:
        """Copy with delivery maps limited to the given tool_call_ids (pending)."""
        out = ConvertedHistory(system=self.system, entries=self.entries)
        out.tool_call_meta = self.tool_call_meta
        for cid, text in self.tool_results.items():
            if cid not in ids:
                continue
            out.tool_results[cid] = text
            meta = self.tool_call_meta.get(cid)
            if meta:
                out.tool_results_by_key[meta] = text
                out.tool_results_by_name[meta[0]] = text
        return out

    @property
    def has_pending_tool_results(self) -> bool:
        return bool(self.tool_results)


def _tool_message_text(msg: ToolMessage) -> str:
    if isinstance(msg.content, str):
        return msg.content
    parts = []
    for item in msg.content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
        else:
            parts.append(str(item))
    return "\n".join(parts)


def convert_lc_messages(
    messages: list[BaseMessage], file_resolver: FileResolver | None = None
) -> ConvertedHistory:
    """Convert LangChain messages into CLI stream entries + tool-result map."""
    out = ConvertedHistory()
    call_names: dict[str, str] = {}
    call_args: dict[str, str] = {}

    for msg in messages:
        if isinstance(msg, SystemMessage):
            text = (
                msg.content
                if isinstance(msg.content, str)
                else "\n".join(
                    i.get("text", "") if isinstance(i, dict) else str(i)
                    for i in msg.content
                )
            )
            out.system = f"{out.system}\n\n{text}" if out.system else text
        elif isinstance(msg, ToolMessage):
            text = _tool_message_text(msg)
            out.tool_results[msg.tool_call_id] = text
            name = call_names.get(msg.tool_call_id)
            if name:
                short = strip_tool_namespace(name)
                out.tool_results_by_name[short] = text
                args_json = call_args.get(msg.tool_call_id)
                if args_json is not None:
                    out.tool_results_by_key[(short, args_json)] = text
        elif isinstance(msg, AIMessage):
            for tc in msg.tool_calls:
                cid = tc["id"]
                if cid is None:
                    continue
                call_names[cid] = tc["name"]
                call_args[cid] = canonical_args(tc["args"])
                out.tool_call_meta[cid] = (
                    strip_tool_namespace(tc["name"]),
                    canonical_args(tc["args"]),
                )
            blocks = _ai_message_to_blocks(msg)
            if blocks:
                out.entries.append(
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": blocks},
                    }
                )
        elif isinstance(msg, HumanMessage):
            out.entries.append(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": _human_content_to_blocks(msg.content, file_resolver),
                    },
                }
            )
        else:  # ChatMessage / unknown role -> user text
            out.entries.append(
                {
                    "type": "user",
                    "message": {"role": "user", "content": str(msg.content)},
                }
            )
    return out


def flatten_to_single_user(entries: list[dict]) -> dict:
    """Structured flatten: whole history as ONE user message (spike S3).

    Multi-message replay triggers a live generation per historical user
    message, so arbitrary histories are collapsed into a single user message.
    Text gets role labels; image/document blocks are preserved verbatim so
    multimodality survives the flatten.
    """
    blocks: list[dict] = [
        {
            "type": "text",
            "text": (
                "The following is the conversation so far. Continue it by "
                "responding to the last user message.\n"
            ),
        }
    ]
    for entry in entries:
        role = entry["message"]["role"]
        label = "User" if role == "user" else "Assistant"
        content = entry["message"]["content"]
        if isinstance(content, str):
            blocks.append({"type": "text", "text": f"[{label}]: {content}"})
            continue
        texts: list[str] = []
        for block in content:
            btype = block.get("type")
            if btype == "text":
                texts.append(block["text"])
            elif btype in ("image", "document"):
                if texts:
                    blocks.append(
                        {"type": "text", "text": f"[{label}]: {' '.join(texts)}"}
                    )
                    texts = []
                blocks.append({"type": "text", "text": f"[{label} attached {btype}]:"})
                blocks.append(block)
            elif btype == "tool_use":
                texts.append(
                    f"<called tool {strip_tool_namespace(block['name'])} "
                    f"with {block['input']}>"
                )
        if texts:
            blocks.append({"type": "text", "text": f"[{label}]: {' '.join(texts)}"})
    return {"type": "user", "message": {"role": "user", "content": blocks}}


# ── CLI -> LangChain ─────────────────────────────────────────


def sdk_blocks_to_lc(
    assistant_messages: list[Any],
    *,
    deferred: bool,
    delivered_ids: set[str] | None = None,
    delivered_keys: set[tuple[str, str]] | None = None,
) -> tuple[str | list[dict], list[dict]]:
    """SDK AssistantMessage blocks -> (LangChain content, tool_calls).

    Only NEW deferred lc-tool calls become tool_calls: a resumed session
    re-emits the tool_use blocks it re-fired and delivered in-run — those are
    satisfied, not requests to the caller — and when the run ended on anything
    other than tool_deferred there is nothing pending at all. Text emitted
    AFTER the first deferred tool_use is dropped (the model reacting to the
    deferral — spike S1).
    """
    delivered_ids = delivered_ids or set()
    delivered_keys = delivered_keys or set()
    content_blocks: list[dict] = []
    tool_calls: list[dict] = []
    saw_deferred_tool_use = False

    for msg in assistant_messages:
        for block in msg.content:
            type_name = type(block).__name__
            if type_name == "TextBlock":
                if deferred and saw_deferred_tool_use:
                    continue
                content_blocks.append({"type": "text", "text": block.text})
            elif type_name == "ThinkingBlock":
                content_blocks.append(
                    {
                        "type": "thinking",
                        "thinking": block.thinking,
                        "signature": getattr(block, "signature", None),
                    }
                )
            elif type_name == "ToolUseBlock":
                if block.name.startswith(MCP_TOOL_PREFIX):
                    short = strip_tool_namespace(block.name)
                    key = (short, canonical_args(block.input))
                    if (
                        not deferred
                        or block.id in delivered_ids
                        or key in delivered_keys
                    ):
                        continue  # satisfied in-run, not a request to the caller
                    saw_deferred_tool_use = True
                    tool_calls.append(
                        {
                            "name": short,
                            "args": block.input,
                            "id": block.id,
                            "type": "tool_call",
                        }
                    )
                else:
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )

    # Simple string content when there's nothing but text
    if all(b["type"] == "text" for b in content_blocks):
        return "".join(b["text"] for b in content_blocks), tool_calls
    return content_blocks, tool_calls


def usage_to_usage_metadata(usage: dict[str, Any] | None) -> UsageMetadata | None:
    if not usage:
        return None
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
    input_total = input_tokens + cache_read + cache_creation
    meta: UsageMetadata = {
        "input_tokens": input_total,
        "output_tokens": output_tokens,
        "total_tokens": input_total + output_tokens,
    }
    if cache_read or cache_creation:
        meta["input_token_details"] = {
            "cache_read": cache_read,
            "cache_creation": cache_creation,
        }
    return meta


def rate_limit_to_meta(event: Any) -> dict[str, Any]:
    """RateLimitEvent -> response_metadata["rate_limit"] dict (spike S6)."""
    info = getattr(event, "rate_limit_info", None) or event
    return {
        "status": getattr(info, "status", None),
        "type": getattr(info, "rate_limit_type", None),
        "utilization": getattr(info, "utilization", None),
        "resets_at": getattr(info, "resets_at", None),
    }


def result_to_response_metadata(result: Any, model: str) -> dict[str, Any]:
    """ResultMessage -> response_metadata (ChatAnthropic-compatible keys + extras)."""
    meta: dict[str, Any] = {
        "model_name": model,
        "model": model,
        "stop_reason": getattr(result, "stop_reason", None),
        "session_id": getattr(result, "session_id", None),
    }
    if getattr(result, "total_cost_usd", None) is not None:
        meta["total_cost_usd"] = result.total_cost_usd
    if getattr(result, "num_turns", None) is not None:
        meta["num_turns"] = result.num_turns
    if getattr(result, "duration_ms", None) is not None:
        meta["duration_ms"] = result.duration_ms
    return meta
