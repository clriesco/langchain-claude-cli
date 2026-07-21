"""Run execution: session resolution, retries, watchdog, result assembly.

Split out of chat_models.py in v0.4 (design D1) — pure refactor, no behavior
change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import ensure_config

from langchain_claude_cli._compat import warn_once
from langchain_claude_cli._convert import (
    ConvertedHistory,
    convert_lc_messages,
    flatten_to_single_user,
    rate_limit_to_meta,
    result_to_response_metadata,
    sdk_blocks_to_lc,
    usage_to_usage_metadata,
)
from langchain_claude_cli._sessions import Resolution
from langchain_claude_cli.exceptions import (
    ClaudeCliBudgetExceededError,
    ClaudeCliError,
    ClaudeCliInterruptedError,
    ClaudeCliOverloadedError,
    ClaudeCliTimeoutError,
    classify_status,
)

if TYPE_CHECKING:
    from langchain_claude_cli.chat_models import ChatClaudeCli

logger = logging.getLogger("langchain_claude_cli")

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}


def _effective_config(config: RunnableConfig | None) -> RunnableConfig:
    """Resolve the RunnableConfig for this call: explicit kwarg, else ambient.

    ``BaseChatModel.invoke/ainvoke`` take ``config`` as their own parameter and
    decompose it into callbacks/tags/metadata — it never reaches ``**kwargs``,
    and ``bind(config=...)`` collides with the positional parameter. So the
    kwarg path alone can only ever be fed by internal callers.

    ``ensure_config()`` reads langchain-core's contextvar, which LangGraph
    populates while running a node — the one place a `thread_id` is actually
    available. Explicit kwarg still wins when present.

    Only ``thread_id`` may be read from the ambient config (via
    ``_thread_key``). ``session_id`` must NOT: the key is overloaded in the
    LangChain ecosystem (``RunnableWithMessageHistory``'s default field spec is
    literally ``session_id``, meaning a chat-history key, not a CLI session
    UUID), so honoring an ambient one would hijack the session with a value
    that was never addressed to this model.
    """
    if config:
        return config
    return ensure_config()


def _is_contradictory_success(result: Any, text: str) -> bool:
    """Detect the CLI's contradictory ``is_error=true`` + ``subtype="success"``.

    Under usage-window pressure the CLI sometimes emits a result flagged as an
    error yet labelled ``success`` with no error text; the SDK turns the ensuing
    non-zero exit into ``Exception("...returned an error result: success")``.
    That is not a genuine error result (it carries no ``api_error_status`` and
    the CLI itself said ``success``), so we must not treat it as terminal.

    Prefer inspecting the already-collected ResultMessage; fall back to the
    exception text when the result was not captured.
    """
    if result is not None and getattr(result, "is_error", False):
        errors = getattr(result, "errors", None) or []
        if not errors and getattr(result, "subtype", None) == "success":
            return True
    return text.rstrip().endswith("returned an error result: success")


def _run_sync(coro: Any) -> Any:
    """Run a coroutine from sync context, surviving an already-running loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        result: list[Any] = [None]
        exc: list[BaseException | None] = [None]

        def _target() -> None:
            try:
                result[0] = asyncio.run(coro)
            except BaseException as e:
                exc[0] = e

        t = threading.Thread(target=_target, daemon=True)
        t.start()
        t.join()
        if exc[0] is not None:
            raise exc[0]
        return result[0]
    return asyncio.run(coro)


def _apply_stop_and_max_tokens(
    content: str | list, stop: list[str] | None, max_tokens: int | None
) -> tuple[str | list, str | None]:
    """Client-side stop_sequences + max_tokens truncation (design D6, level B).

    Returns (content, synthetic_stop_reason or None).
    """

    def _cut(text: str) -> tuple[str, str | None]:
        reason = None
        if stop:
            idxs = [text.find(s) for s in stop if text.find(s) != -1]
            if idxs:
                text = text[: min(idxs)]
                reason = "stop_sequence"
        if max_tokens is not None and len(text) > max_tokens * 4:
            text = text[: max_tokens * 4]
            reason = "max_tokens"
        return text, reason

    if isinstance(content, str):
        return _cut(content)
    reason = None
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and reason is None:
            text, reason = _cut(block["text"])
            out.append({**block, "text": text})
        elif reason is None:
            out.append(block)
    return out, reason


class _RunnerMixin:
    """Query execution and result assembly for ChatClaudeCli."""

    def _effective_inactivity(self: ChatClaudeCli) -> float | None:
        if self.inactivity_timeout == "auto":
            return None if self.builtin_tools is not None else 120.0
        return self.inactivity_timeout

    # ── Session / prompt resolution ──────────────────────────

    def _resolve_session(
        self: ChatClaudeCli,
        messages: list[BaseMessage],
        config: RunnableConfig | None,
    ) -> Resolution:
        # session_id only from the explicit kwarg (internal callers) or the
        # constructor — never from the ambient config, where the same key name
        # means "chat-history key" to RunnableWithMessageHistory and friends.
        configurable = (config or {}).get("configurable") or {}
        explicit = configurable.get("session_id") or self.session_id
        if explicit:
            # Caller manages history: send only the last message as suffix.
            return Resolution(
                strategy="resume", session_id=explicit, suffix=messages[-1:]
            )
        return self._session_cache.resolve(messages, thread_id=self._thread_key(config))

    def _thread_key(self: ChatClaudeCli, config: RunnableConfig | None) -> str | None:
        """Namespaced recovery key: ``<stable profile digest>:<thread_id>``.

        Returns None when no thread_id is reachable, leaving the prefix
        fingerprint as the only resolution path.
        """
        config = _effective_config(config)
        thread_id = (config.get("configurable") or {}).get("thread_id")
        if not thread_id:
            return None
        return f"{self._session_profile()}:{thread_id}"

    def _build_prompt_entries(
        self: ChatClaudeCli, resolution: Resolution, converted: ConvertedHistory
    ) -> list[dict]:
        entries = converted.entries
        if resolution.strategy == "resume":
            # Suffix entries only; pending tool results ride the MCP handlers.
            # Assistant entries in a suffix can't be replayed cheaply — flatten them.
            if any(e["type"] == "assistant" for e in entries):
                return [flatten_to_single_user(entries)]
            return entries
        user_count = sum(1 for e in entries if e["type"] == "user")
        has_assistant = any(e["type"] == "assistant" for e in entries)
        if self.history_mode == "replay" and (has_assistant or user_count > 1):
            warn_once(
                "history_replay",
                "history_mode='replay' is EXPERIMENTAL: each historical user "
                "message triggers a live generation (cost grows with history "
                "length) and the model may prefer its own live replies over "
                "the injected assistant turns (fidelity is race-dependent).",
            )
            return entries
        if self.history_mode == "flatten" or has_assistant or user_count > 1:
            if entries:
                warn_once(
                    "history_flatten",
                    "ChatClaudeCli received an arbitrary history with no known "
                    "session prefix; it was flattened into a single user message "
                    "(multimodal blocks preserved). Growing conversations resume "
                    "their CLI session with full fidelity.",
                ) if has_assistant or user_count > 1 else None
                return [flatten_to_single_user(entries)]
            return entries
        return entries

    # ── Core async run ───────────────────────────────────────

    async def _arun_query(
        self: ChatClaudeCli,
        messages: list[BaseMessage],
        stop: list[str] | None,
        **kwargs: Any,
    ) -> tuple[list[Any], Any, ConvertedHistory, Resolution, dict[str, Any] | None]:
        """Run one CLI query; registered as an interruptible active run (v0.4).

        interrupt() cancels the underlying task; the cancellation surfaces
        here as ClaudeCliInterruptedError (subprocess cleanup is guaranteed
        by the stream-close finally inside the run).
        """
        token = SimpleNamespace(
            loop=asyncio.get_running_loop(),
            task=asyncio.current_task(),
            session_id=None,
            interrupted=False,
        )
        self._active_runs[id(token)] = token
        try:
            return await self._arun_query_inner(
                messages, stop, _run_token=token, **kwargs
            )
        except asyncio.CancelledError:
            if token.interrupted:
                raise ClaudeCliInterruptedError(
                    "run cancelled via interrupt()"
                ) from None
            raise
        finally:
            self._active_runs.pop(id(token), None)

    async def _arun_query_inner(
        self: ChatClaudeCli,
        messages: list[BaseMessage],
        stop: list[str] | None,
        **kwargs: Any,
    ) -> tuple[list[Any], Any, ConvertedHistory, Resolution, dict[str, Any] | None]:
        """Run one CLI query with retries/timeout; return (assistant_msgs, result, ...)."""
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                RateLimitEvent,
                ResultMessage,
                query,
            )
        except ImportError as e:
            raise ImportError(
                "claude-agent-sdk is required. Install with: pip install claude-agent-sdk"
            ) from e

        tool_schemas = kwargs.get("tools") or []
        resolution = self._resolve_session(messages, kwargs.get("config"))
        converted = convert_lc_messages(resolution.suffix, self._file_resolver)
        # Delivery = ONLY the suffix's pending ToolMessages (older results were
        # already consumed in-session; new same-args calls must defer, not get
        # stale results). Names/args come from AIMessages in the full history.
        pending_ids = {
            m.tool_call_id for m in resolution.suffix if isinstance(m, ToolMessage)
        }
        delivery = convert_lc_messages(messages).restrict_results(pending_ids)
        entries = self._build_prompt_entries(resolution, converted)
        if not entries and not delivery.tool_results:
            # Nothing would drive the CLI turn (e.g. exact-duplicate history):
            # degrade to a fresh flattened run instead of hanging on stdin.
            resolution = Resolution(strategy="new", suffix=list(messages))
            converted = convert_lc_messages(messages)
            entries = self._build_prompt_entries(resolution, converted)

        run_token = kwargs.pop("_run_token", None)
        if run_token is not None:
            run_token.session_id = resolution.session_id

        logger.debug(
            "session: %s%s, suffix=%d msgs, pending_tools=%d",
            resolution.strategy,
            f" ({resolution.session_id[:8]}…)" if resolution.session_id else "",
            len(resolution.suffix),
            len(delivery.tool_results),
        )

        tool_choice = kwargs.get("tool_choice")
        system = converted.system
        if tool_choice not in (None, "auto") and not (
            isinstance(tool_choice, dict) and tool_choice.get("type") == "auto"
        ):
            name = (
                tool_choice.get("name")
                if isinstance(tool_choice, dict)
                else (None if tool_choice == "any" else tool_choice)
            )
            instruction = (
                f"You MUST call the tool `{name}` to answer."
                if name
                else "You MUST call at least one of the provided tools to answer."
            )
            system = f"{system}\n\n{instruction}" if system else instruction

        # Persistent fast path (D4): plain conversation turn on a live client.
        if (
            self._pool is not None
            and resolution.strategy == "resume"
            and resolution.session_id
            and not tool_schemas
            and not delivery.tool_results
            and not kwargs.get("output_format")
            and entries
        ):
            pooled = await self._pool.run_turn(
                resolution.session_id, self._options_sig(), entries
            )
            if pooled is not None:
                pooled_msgs, pooled_result, pooled_rate = pooled
                return pooled_msgs, pooled_result, delivery, resolution, pooled_rate

        options = self._build_options(
            system=system,
            resume=resolution.session_id,
            tool_schemas=tool_schemas,
            delivery=delivery,
            server_builtin_tools=kwargs.get("server_builtin_tools"),
            output_format=kwargs.get("output_format"),
        )

        async def _stream_entries() -> AsyncIterator[dict]:
            for entry in entries:
                yield entry

        attempts = max(1, self.max_retries + 1)
        last_error: Exception | None = None
        for attempt in range(attempts):
            collected: dict[str, Any] = {"result": None, "msgs": [], "rate_limit": None}
            inactivity = self._effective_inactivity()

            async def _collect2() -> None:
                stream = query(prompt=_stream_entries(), options=options)
                iterator = stream.__aiter__()
                try:
                    while True:
                        try:
                            if inactivity is not None:
                                msg = await asyncio.wait_for(
                                    iterator.__anext__(), timeout=inactivity
                                )
                            else:
                                msg = await iterator.__anext__()
                        except StopAsyncIteration:
                            break
                        except (
                            TimeoutError,
                            asyncio.TimeoutError,
                        ):  # 3.10: distinct classes
                            logger.warning(
                                "watchdog: no SDK activity for %.0fs, aborting run",
                                inactivity,
                            )
                            raise ClaudeCliTimeoutError(
                                f"no SDK activity for {inactivity}s "
                                "(inactivity_timeout; the CLI process may have "
                                "died — see README reliability notes)"
                            ) from None
                        if isinstance(msg, AssistantMessage):
                            collected["msgs"].append(msg)
                        elif isinstance(msg, ResultMessage):
                            collected["result"] = msg
                        elif isinstance(msg, RateLimitEvent):
                            collected["rate_limit"] = rate_limit_to_meta(msg)
                finally:
                    # Close INSIDE the still-running loop: on timeout/cancel,
                    # skipping this leaves an orphaned `claude` subprocess
                    # (the SDK's cleanup tasks die with the loop) that keeps
                    # consuming quota and contends with the next run.
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(cast(Any, stream).aclose(), timeout=5)

            try:
                if self.default_request_timeout:
                    await asyncio.wait_for(
                        _collect2(), timeout=self.default_request_timeout
                    )
                else:
                    await _collect2()
            except ClaudeCliTimeoutError:
                raise  # inactivity watchdog: already typed and logged
            except (TimeoutError, asyncio.TimeoutError) as e:  # 3.10: distinct classes
                raise ClaudeCliTimeoutError(
                    f"run exceeded timeout={self.default_request_timeout}s"
                ) from e
            except Exception as e:
                text = str(e)
                if "maximum budget" in text.lower():
                    # Deliberate, user-set limit — terminal, never retried.
                    raise ClaudeCliBudgetExceededError(text) from e
                if "returned an error result" in text:
                    err_result = collected["result"]
                    if _is_contradictory_success(err_result, text):
                        # The CLI labelled the outcome "success" yet flagged
                        # is_error (intermittent, account-level — see the bug
                        # spec). Never surface this as a fatal untyped Exception.
                        if collected["msgs"]:
                            # Option A: the turn's assistant messages were
                            # already collected before the trailing error
                            # sentinel — recover them as the success the CLI
                            # reported. Return directly: falling through to the
                            # post-collection block below would re-raise on
                            # is_error (a "success" result has no HTTP status).
                            return (
                                collected["msgs"],
                                err_result,
                                delivery,
                                resolution,
                                collected["rate_limit"],
                            )
                        # Option B: nothing to recover — raise a typed,
                        # retryable error so the retry/fallback policy owns it,
                        # instead of a fatal untyped throw.
                        raise ClaudeCliOverloadedError(
                            "contradictory CLI result "
                            "(is_error=true, subtype=success) with no assistant "
                            "messages; treating as transient"
                        ) from e
                    # Genuine CLI error result (error_max_turns,
                    # error_during_execution, ...): not a transport failure —
                    # retrying would just repeat the same failing run.
                    raise
                last_error = e  # transport/process error: retry
                if attempt + 1 < attempts:
                    logger.info(
                        "transport error (%s), retry %d/%d",
                        type(e).__name__,
                        attempt + 1,
                        attempts - 1,
                    )
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                raise

            result: Any = collected["result"]
            status = getattr(result, "api_error_status", None) if result else None
            detail = str(
                (getattr(result, "result", None) or getattr(result, "errors", None))
                if result
                else ""
            )
            if result is not None and result.is_error and status in _RETRYABLE_STATUS:
                last_error = classify_status(status, detail or f"status {status}")
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                # No attempts left: raise, never return the error result as a
                # normal (empty) completion (reported downstream: silent empty
                # AIMessage with max_retries=0 on a single 429/529).
                raise last_error
            if (
                result is not None
                and result.is_error
                and status not in _RETRYABLE_STATUS
            ):
                raise classify_status(status, detail or str(result.subtype))
            return (
                collected["msgs"],
                result,
                delivery,
                resolution,
                collected["rate_limit"],
            )

        raise last_error or ClaudeCliError("Claude CLI run failed")

    def _build_chat_result(
        self: ChatClaudeCli,
        assistant_msgs: list[Any],
        result: Any,
        messages: list[BaseMessage],
        stop: list[str] | None,
        delivery: ConvertedHistory | None = None,
        rate_limit: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        deferred = bool(
            result and getattr(result, "stop_reason", None) == "tool_deferred"
        )
        content, tool_calls = sdk_blocks_to_lc(
            assistant_msgs,
            deferred=deferred,
            delivered_ids=set(delivery.tool_results) if delivery else None,
            delivered_keys=set(delivery.tool_results_by_key) if delivery else None,
        )

        stop_list = list(stop or []) + list(self.stop_sequences or [])
        synthetic_reason = None
        if not deferred:
            content, synthetic_reason = _apply_stop_and_max_tokens(
                content, stop_list or None, self.max_tokens
            )

        response_metadata = (
            result_to_response_metadata(result, self.model) if result else {}
        )
        if synthetic_reason:
            response_metadata["stop_reason"] = synthetic_reason
        if rate_limit:
            response_metadata["rate_limit"] = rate_limit

        additional_kwargs: dict[str, Any] = {}
        structured = getattr(result, "structured_output", None) if result else None
        if structured is not None:
            additional_kwargs["structured_output"] = structured

        ai_msg = AIMessage(
            content=cast(Any, content),
            tool_calls=tool_calls,
            usage_metadata=usage_to_usage_metadata(
                getattr(result, "usage", None) if result else None
            ),
            response_metadata=response_metadata,
            additional_kwargs=additional_kwargs,
        )

        session_id = getattr(result, "session_id", None) if result else None
        if session_id:
            self._session_cache.register(
                [*messages, ai_msg],
                session_id,
                thread_id=self._thread_key(kwargs.get("config")),
            )
            if self._pool is not None and not kwargs.get("tools") and not deferred:
                # Warm a live client for the next turn (fire-and-forget).
                warm_options = self._build_options(
                    system=None,
                    resume=session_id,
                    tool_schemas=None,
                    delivery=ConvertedHistory(),
                )
                self._pool.warm(session_id, warm_options, self._options_sig())

        return ChatResult(
            generations=[
                ChatGeneration(message=ai_msg, generation_info=response_metadata)
            ]
        )

    async def _agenerate(
        self: ChatClaudeCli,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        tool_choice = kwargs.get("tool_choice")
        forced = tool_choice not in (None, "auto") and not (
            isinstance(tool_choice, dict) and tool_choice.get("type") == "auto"
        )
        assistant_msgs, result, delivery, _, rate_limit = await self._arun_query(
            messages, stop, **kwargs
        )
        chat_result = self._build_chat_result(
            assistant_msgs, result, messages, stop, delivery, rate_limit, **kwargs
        )
        msg = cast(AIMessage, chat_result.generations[0].message)
        if forced and not msg.tool_calls:
            # One retry with the instruction already embedded (design D3)
            assistant_msgs, result, delivery, _, rate_limit = await self._arun_query(
                messages, stop, **kwargs
            )
            chat_result = self._build_chat_result(
                assistant_msgs, result, messages, stop, delivery, rate_limit, **kwargs
            )
            msg = cast(AIMessage, chat_result.generations[0].message)
            if not msg.tool_calls:
                raise RuntimeError(
                    f"tool_choice={tool_choice!r} could not be satisfied: the model "
                    "did not call the required tool (CLI cannot force tool use)."
                )
        return chat_result

    def _generate(
        self: ChatClaudeCli,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return _run_sync(self._agenerate(messages, stop, None, **kwargs))
