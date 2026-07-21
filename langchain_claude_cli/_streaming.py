"""Streaming: raw SDK StreamEvents -> AIMessageChunk translation.

Split out of chat_models.py in v0.4 (design D1) — pure refactor, no behavior
change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any, cast

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk

from langchain_claude_cli._convert import (
    MCP_TOOL_PREFIX,
    convert_lc_messages,
    rate_limit_to_meta,
    result_to_response_metadata,
    sdk_blocks_to_lc,
    strip_tool_namespace,
    usage_to_usage_metadata,
)
from langchain_claude_cli._sessions import Resolution

if TYPE_CHECKING:
    from langchain_claude_cli.chat_models import ChatClaudeCli

logger = logging.getLogger("langchain_claude_cli")


class _StreamingMixin:
    """Token-by-token streaming for ChatClaudeCli."""

    async def _astream(
        self: ChatClaudeCli,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                RateLimitEvent,
                ResultMessage,
                StreamEvent,
                query,
            )
        except ImportError as e:
            raise ImportError(
                "claude-agent-sdk is required. Install with: pip install claude-agent-sdk"
            ) from e

        tool_schemas = kwargs.get("tools") or []
        resolution = self._resolve_session(messages, kwargs.get("config"))
        converted = convert_lc_messages(resolution.suffix, self._file_resolver)
        pending_ids = {
            m.tool_call_id for m in resolution.suffix if isinstance(m, ToolMessage)
        }
        delivery = convert_lc_messages(messages).restrict_results(pending_ids)
        entries = self._build_prompt_entries(resolution, converted)
        if not entries and not delivery.tool_results:
            resolution = Resolution(strategy="new", suffix=list(messages))
            converted = convert_lc_messages(messages)
            entries = self._build_prompt_entries(resolution, converted)
        options = self._build_options(
            system=converted.system,
            resume=resolution.session_id,
            tool_schemas=tool_schemas,
            delivery=delivery,
            server_builtin_tools=kwargs.get("server_builtin_tools"),
            output_format=kwargs.get("output_format"),
            include_partial_messages=True,
        )

        async def _stream_entries() -> AsyncIterator[dict]:
            for entry in entries:
                yield entry

        stop_list = list(stop or []) + list(self.stop_sequences or [])
        max_hold = max((len(s) for s in stop_list), default=1) - 1
        pending = ""  # holdback buffer for stop-sequence detection
        emitted_chars = 0
        stopped = False
        tool_block_index: dict[int, str] = {}  # api index -> lc tool_use id
        # api index -> in-flight builtin/MCP tool activity (agentic mode)
        server_tools: dict[int, dict[str, Any]] = {}
        # synthetic content-block index for emitted tool_use blocks: api indexes
        # restart on every assistant message within a run, and LangChain merges
        # list blocks by index — a fresh monotonic index avoids collisions.
        server_tool_seq = 1000
        assistant_msgs: list[Any] = []
        final_result: Any = None
        stream_rate_limit: dict[str, Any] | None = None

        def _scan(text: str) -> tuple[str, bool]:
            """Return (emittable_text, hit_stop)."""
            nonlocal pending
            pending += text
            for s in stop_list:
                idx = pending.find(s)
                if idx != -1:
                    out = pending[:idx]
                    pending = ""
                    return out, True
            if max_hold > 0 and len(pending) > max_hold:
                out, pending_new = pending[:-max_hold], pending[-max_hold:]
                pending = pending_new
                return out, False
            if max_hold == 0:
                out, pending = pending, ""
                return out, False
            return "", False

        stream = query(prompt=_stream_entries(), options=options)
        try:
            async for msg in stream:
                if stopped:
                    break
                if isinstance(msg, StreamEvent):
                    event = msg.event
                    etype = event.get("type", "")
                    if etype == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            idx = event.get("index", 0)
                            name = str(block.get("name", ""))
                            if name.startswith(MCP_TOOL_PREFIX):
                                tool_block_index[idx] = block.get("id", "")
                                chunk = ChatGenerationChunk(
                                    message=AIMessageChunk(
                                        content="",
                                        tool_call_chunks=[
                                            {
                                                "name": strip_tool_namespace(name),
                                                "args": "",
                                                "id": block.get("id"),
                                                "index": idx,
                                                "type": "tool_call_chunk",
                                            }
                                        ],
                                    )
                                )
                                if run_manager:
                                    await run_manager.on_llm_new_token("", chunk=chunk)
                                yield chunk
                            else:
                                # Builtin/MCP tool executed in-run (agentic mode):
                                # buffer until its input JSON is complete.
                                server_tools[idx] = {
                                    "id": block.get("id", ""),
                                    "name": name,
                                    "parts": [],
                                }
                    elif etype == "content_block_delta":
                        delta = event.get("delta", {})
                        dtype = delta.get("type", "")
                        if dtype == "text_delta":
                            text, hit = _scan(delta.get("text", ""))
                            if text:
                                emitted_chars += len(text)
                                if (
                                    self.max_tokens is not None
                                    and emitted_chars > self.max_tokens * 4
                                ):
                                    text = text[: self.max_tokens * 4 - emitted_chars]
                                    hit = True
                                chunk = ChatGenerationChunk(
                                    message=AIMessageChunk(content=text)
                                )
                                if run_manager:
                                    await run_manager.on_llm_new_token(
                                        text, chunk=chunk
                                    )
                                yield chunk
                            if hit:
                                stopped = True
                        elif dtype == "thinking_delta":
                            chunk = ChatGenerationChunk(
                                message=AIMessageChunk(
                                    content=[
                                        {
                                            "type": "thinking",
                                            "thinking": delta.get("thinking", ""),
                                            "index": event.get("index", 0),
                                        }
                                    ]
                                )
                            )
                            yield chunk
                        elif dtype == "input_json_delta":
                            idx = event.get("index", 0)
                            if idx in server_tools:
                                server_tools[idx]["parts"].append(
                                    delta.get("partial_json", "")
                                )
                            elif idx in tool_block_index:
                                chunk = ChatGenerationChunk(
                                    message=AIMessageChunk(
                                        content="",
                                        tool_call_chunks=[
                                            {
                                                "name": None,
                                                "args": delta.get("partial_json", ""),
                                                "id": None,
                                                "index": idx,
                                                "type": "tool_call_chunk",
                                            }
                                        ],
                                    )
                                )
                                yield chunk
                    elif etype == "content_block_stop":
                        idx = event.get("index", 0)
                        tool_block_index.pop(idx, None)
                        info = server_tools.pop(idx, None)
                        if info is not None:
                            # Completed builtin/MCP tool call: emit one
                            # tool_use content block (agentic activity).
                            try:
                                tool_input = json.loads("".join(info["parts"]) or "{}")
                            except json.JSONDecodeError:
                                tool_input = {"_raw": "".join(info["parts"])}
                            server_tool_seq += 1
                            yield ChatGenerationChunk(
                                message=AIMessageChunk(
                                    content=[
                                        {
                                            "type": "tool_use",
                                            "id": info["id"],
                                            "name": info["name"],
                                            "input": tool_input,
                                            "index": server_tool_seq,
                                        }
                                    ]
                                )
                            )
                elif isinstance(msg, AssistantMessage):
                    assistant_msgs.append(msg)
                elif isinstance(msg, ResultMessage):
                    final_result = msg
                elif isinstance(msg, RateLimitEvent):
                    stream_rate_limit = rate_limit_to_meta(msg)
        finally:
            await cast(Any, stream).aclose()

        if pending and not stopped:
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=pending))
            if run_manager:
                await run_manager.on_llm_new_token(pending, chunk=chunk)
            yield chunk

        # Final chunk: usage + response metadata (parity with stream_usage=True)
        if final_result is not None:
            meta = result_to_response_metadata(final_result, self.model)
            if stopped:
                meta["stop_reason"] = "stop_sequence"
            if stream_rate_limit:
                meta["rate_limit"] = stream_rate_limit
            usage = (
                usage_to_usage_metadata(getattr(final_result, "usage", None))
                if self.stream_usage
                else None
            )
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="", usage_metadata=usage, response_metadata=meta
                )
            )
            session_id = getattr(final_result, "session_id", None)
            if session_id and not stopped:
                content, tool_calls = sdk_blocks_to_lc(
                    assistant_msgs,
                    deferred=getattr(final_result, "stop_reason", None)
                    == "tool_deferred",
                    delivered_ids=set(delivery.tool_results),
                    delivered_keys=set(delivery.tool_results_by_key),
                )
                self._session_cache.register(
                    [
                        *messages,
                        AIMessage(content=cast(Any, content), tool_calls=tool_calls),
                    ],
                    session_id,
                    thread_id=self._thread_key(kwargs.get("config")),
                )

    def _stream(
        self: ChatClaudeCli,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """Sync streaming via background thread + queue (safe inside a running loop)."""
        _DONE = object()
        q: queue.Queue = queue.Queue()

        async def _pump() -> None:
            try:
                async for chunk in self._astream(messages, stop, None, **kwargs):
                    q.put(chunk)
            except BaseException as e:
                q.put(e)
            finally:
                q.put(_DONE)

        thread = threading.Thread(target=lambda: asyncio.run(_pump()), daemon=True)
        thread.start()
        while True:
            item = q.get()
            if item is _DONE:
                break
            if isinstance(item, BaseException):
                raise item
            if (
                run_manager
                and isinstance(item.message.content, str)
                and item.message.content
            ):
                run_manager.on_llm_new_token(item.message.content, chunk=item)
            yield item
        thread.join()
