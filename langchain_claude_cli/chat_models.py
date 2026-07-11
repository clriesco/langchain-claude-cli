"""ChatClaudeCli — ChatAnthropic drop-in backed by the Claude Code CLI.

Uses your Claude Pro/Max subscription via claude-agent-sdk; no API key.
Parity policy (design D6): native where the SDK supports it, client-side
workaround where it doesn't, accepted-no-op (with a one-shot warning) where
the CLI cannot express it. The constructor never raises for a parameter that
ChatAnthropic accepts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from typing import Any, Literal, cast

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from langchain_claude_cli._compat import warn_once
from langchain_claude_cli._convert import (
    MCP_SERVER_NAME,
    MCP_TOOL_PREFIX,
    ConvertedHistory,
    add_tool_namespace,
    canonical_args,
    convert_lc_messages,
    flatten_to_single_user,
    rate_limit_to_meta,
    result_to_response_metadata,
    sdk_blocks_to_lc,
    strip_tool_namespace,
    usage_to_usage_metadata,
)
from langchain_claude_cli._pool import ClientPool
from langchain_claude_cli._sessions import Resolution, SessionCache, make_store
from langchain_claude_cli.exceptions import (
    ClaudeCliBudgetExceededError,
    ClaudeCliError,
    ClaudeCliTimeoutError,
    classify_status,
)
from langchain_claude_cli.tools import ClaudeTool, normalize_tools

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}

# Auth vars the CLI prefers over the OAuth login when inherited (spike S9)
_AUTH_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# ChatAnthropic server tools -> CLI built-in tools (design/spec agentic-mode)
_SERVER_TOOL_MAP = {
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
}


def _lc_tool_to_anthropic(
    tool: Mapping[str, Any] | type | Callable | BaseTool,
) -> dict[str, Any]:
    """Any LangChain-acceptable tool -> Anthropic schema {name, description, input_schema}."""
    if isinstance(tool, Mapping):
        if "input_schema" in tool:  # already Anthropic format
            return dict(tool)
        if "function" in tool:  # OpenAI format
            fn = tool["function"]
            return {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
        return dict(tool)
    oai = convert_to_openai_tool(tool)["function"]
    return {
        "name": oai["name"],
        "description": oai.get("description", ""),
        "input_schema": oai.get("parameters", {"type": "object", "properties": {}}),
    }


def _is_server_tool(tool: Any) -> str | None:
    """Return the CLI builtin name if `tool` is an Anthropic server-tool schema."""
    if isinstance(tool, Mapping) and isinstance(tool.get("type"), str):
        for prefix, builtin in _SERVER_TOOL_MAP.items():
            if tool["type"].startswith(prefix):
                return builtin
    return None


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


class ChatClaudeCli(BaseChatModel):
    """LangChain chat model using the Claude Code CLI — no API key needed.

    Drop-in replacement for ``langchain_anthropic.ChatAnthropic`` running on a
    Claude Pro/Max subscription. Pure-LLM semantics by default (no built-in
    tools, no filesystem). Agentic mode is opt-in via ``builtin_tools``.
    """

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    # ── ChatAnthropic-parity: native ─────────────────────────
    model: str = Field(default="claude-sonnet-4-5", alias="model_name")
    thinking: dict[str, Any] | None = None
    effort: Literal["max", "xhigh", "high", "medium", "low"] | None = None
    betas: list[str] | None = None
    max_retries: int = 2
    default_request_timeout: float | None = Field(None, alias="timeout")
    streaming: bool = False
    stream_usage: bool = True
    mcp_servers: list[dict[str, Any]] | dict[str, Any] | None = None

    # ── ChatAnthropic-parity: client-side workaround (level B) ─
    max_tokens: int | None = Field(default=None, alias="max_tokens_to_sample")
    stop_sequences: list[str] | None = Field(None, alias="stop")

    # ── ChatAnthropic-parity: accepted no-op (level C) ────────
    temperature: float | None = None
    top_k: int | None = None
    top_p: float | None = None
    anthropic_api_url: str | None = Field(None, alias="base_url")
    anthropic_api_key: Any = None
    anthropic_proxy: str | None = None
    default_headers: Mapping[str, str] | None = None
    model_kwargs: dict[str, Any] = Field(default_factory=dict)
    context_management: dict[str, Any] | None = None
    inference_geo: str | None = None
    output_config: dict[str, Any] | None = None

    # ── Claude CLI specific ──────────────────────────────────
    system_prompt: str | None = None
    """Extra system prompt prepended to any SystemMessage."""
    builtin_tools: list[str | ClaudeTool] | Literal["claude_code"] | None = None
    """CLI built-in tools. None = pure-LLM mode (no tools). "claude_code" = all."""
    max_turns: int | None = None
    """CLI turns per run. Defaults to 1 in pure-LLM mode, unlimited in agentic."""
    permission_mode: (
        Literal["default", "acceptEdits", "plan", "bypassPermissions"] | None
    ) = None
    cwd: str | None = None
    add_dirs: list[str] | None = None
    cli_path: str | None = None
    env: dict[str, str] | None = None
    allowed_tools: list[str | ClaudeTool] | None = None
    disallowed_tools: list[str | ClaudeTool] | None = None
    session_id: str | None = None
    """Explicit CLI session to resume (also settable per-call via config)."""
    auth: Literal["oauth", "inherit"] = "oauth"
    """"oauth" (default) guarantees the CLI subprocess uses your subscription
    login: inherited ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN are neutralized
    (the CLI would otherwise prefer them and bill per token — spike S9).
    "inherit" keeps the process environment as-is."""
    max_budget_usd: float | None = None
    sandbox: dict[str, Any] | None = None
    fallback_model: str | None = None
    history_mode: Literal["auto", "flatten", "replay"] = "auto"
    """"auto": resume by prefix, flatten unknown histories. "flatten": always
    flatten. "replay": replay unknown histories faithfully as multi-message
    input — full role fidelity at the cost of one live generation per
    historical user message (spike S3)."""
    session_store: Any = "memory"
    """Prefix-cache backend: "memory" (default), "file" (persistent JSON in
    ~/.langchain-claude-cli/, conversations survive restarts) or a
    SessionStoreBackend instance."""
    persistent: bool = False
    """Keep a live ClaudeSDKClient per conversation: multi-turn resumes skip
    the subprocess restart (~2x faster per reused turn, spike S8) and enable
    interrupt()/set_model(). Plain conversation turns only — tool-calling
    cycles use the stateless path. For processes with a controlled lifetime."""
    pool_max_clients: int = 4
    pool_ttl: float = 300.0

    _session_cache: SessionCache = PrivateAttr(default=None)  # type: ignore[assignment]
    _pool: ClientPool | None = PrivateAttr(default=None)

    # ── Compat warnings ──────────────────────────────────────

    _NOOP_PARAMS = (
        "temperature",
        "top_k",
        "top_p",
        "anthropic_api_url",
        "anthropic_proxy",
        "default_headers",
        "context_management",
        "inference_geo",
        "output_config",
    )

    @model_validator(mode="after")
    def _finish_init(self) -> ChatClaudeCli:
        for name in self._NOOP_PARAMS:
            if getattr(self, name) not in (None, {}, []):
                warn_once(
                    name,
                    f"ChatClaudeCli accepts `{name}` for ChatAnthropic compatibility "
                    "but the Claude CLI does not support it; the parameter is ignored.",
                )
        self._session_cache = SessionCache(store=make_store(self.session_store))
        if self.persistent:
            self._pool = ClientPool(self.pool_max_clients, self.pool_ttl)
        return self

    def _options_sig(self) -> str:
        """Client reuse is only valid when the run configuration matches."""
        return json.dumps(
            [
                self.model,
                self.effort,
                self.thinking,
                self.system_prompt,
                self.builtin_tools,
                self.permission_mode,
                self.cwd,
            ],
            default=str,
        )

    def interrupt(self, session_id: str | None = None) -> None:
        """Cancel the active run of a persistent conversation (persistent=True)."""
        if self._pool is None:
            raise ClaudeCliError("interrupt() requires persistent=True")
        self._pool.interrupt(session_id)

    def set_session_model(
        self, model: str | None, session_id: str | None = None
    ) -> None:
        """Hot-swap the model of a persistent conversation (persistent=True)."""
        if self._pool is None:
            raise ClaudeCliError("set_session_model() requires persistent=True")
        self._pool.set_model(model, session_id)

    # ── LangChain plumbing ───────────────────────────────────

    @property
    def _llm_type(self) -> str:
        return "chat-claude-cli"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "thinking": self.thinking,
            "effort": self.effort,
            "max_turns": self.max_turns,
            "builtin_tools": self.builtin_tools,
            "permission_mode": self.permission_mode,
        }

    def get_num_tokens_from_messages(
        self,
        messages: list[BaseMessage],
        tools: Sequence | None = None,
        **kwargs: Any,
    ) -> int:
        """Heuristic estimate (~4 chars/token; images ~1600 tokens).

        The CLI has no count_tokens endpoint without an API key.
        """
        warn_once(
            "get_num_tokens_from_messages",
            "ChatClaudeCli.get_num_tokens_from_messages returns a heuristic "
            "estimate; exact counts require the Anthropic API.",
        )
        total = 0
        for msg in messages:
            content = msg.content
            if isinstance(content, str):
                total += len(content) // 4
            else:
                for item in content:
                    if isinstance(item, str):
                        total += len(item) // 4
                    elif isinstance(item, dict):
                        if item.get("type") in ("image", "image_url"):
                            total += 1600
                        elif item.get("type") == "document":
                            total += len(str(item.get("source", ""))) // 6
                        else:
                            total += len(str(item)) // 4
        return total

    # ── Tool binding (group 6) ───────────────────────────────

    def bind_tools(
        self,
        tools: Sequence[Mapping[str, Any] | type | Callable | BaseTool],
        *,
        tool_choice: dict[str, str] | str | None = None,
        parallel_tool_calls: bool | None = None,
        strict: bool | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        """Bind tools; the model returns AIMessage.tool_calls without executing.

        Implemented via in-process MCP + PreToolUse defer (design D3). Server
        tools (web_search/web_fetch schemas) map to CLI built-ins instead.
        """
        if strict is not None:
            warn_once(
                "strict",
                "ChatClaudeCli accepts `strict` but the CLI cannot enforce "
                "strict schema adherence; it is ignored.",
            )
        if parallel_tool_calls is False:
            warn_once(
                "parallel_tool_calls",
                "ChatClaudeCli cannot disable parallel tool use; "
                "`parallel_tool_calls=False` is ignored.",
            )
        schemas: list[dict[str, Any]] = []
        server_builtins: list[str] = []
        for t in tools:
            builtin = _is_server_tool(t)
            if builtin:
                server_builtins.append(builtin)
            else:
                schemas.append(_lc_tool_to_anthropic(t))
        bind_kwargs: dict[str, Any] = {"tools": schemas, **kwargs}
        if tool_choice is not None:
            bind_kwargs["tool_choice"] = tool_choice
        if server_builtins:
            bind_kwargs["server_builtin_tools"] = server_builtins
        return super().bind(**bind_kwargs)

    # ── Structured output (group 7) ──────────────────────────

    def with_structured_output(
        self,
        schema: dict | type,
        *,
        include_raw: bool = False,
        method: Literal["function_calling", "json_schema"] = "function_calling",
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, dict | BaseModel]:
        """Structured output via the CLI's native output_format (json_schema)."""
        from langchain_core.runnables import RunnableLambda

        pydantic_schema: type[BaseModel] | None = (
            schema
            if isinstance(schema, type) and issubclass(schema, BaseModel)
            else None
        )
        if pydantic_schema is not None:
            json_schema = pydantic_schema.model_json_schema()
            name = pydantic_schema.__name__
        else:
            anthropic = _lc_tool_to_anthropic(schema)
            json_schema = anthropic.get("input_schema", anthropic)
            name = anthropic.get("name", "output")

        if method == "function_calling":
            # Same native mechanism; kept for signature compatibility. The CLI's
            # output_format is strictly better than emulating a forced tool call.
            pass

        bound = self.bind(
            output_format={"type": "json_schema", "schema": json_schema},
            ls_structured_output_format={
                "kwargs": {"method": method},
                "schema": {"name": name, "schema": json_schema},
            },
        )

        def _parse(msg: AIMessage) -> Any:
            raw_obj = msg.additional_kwargs.get("structured_output")
            err: Exception | None = None
            parsed: Any = None
            try:
                if raw_obj is None:
                    content = msg.content
                    text = (
                        content
                        if isinstance(content, str)
                        else "".join(
                            b.get("text", "")
                            for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    )
                    raw_obj = json.loads(text)
                parsed = (
                    pydantic_schema.model_validate(raw_obj)
                    if pydantic_schema is not None
                    else raw_obj
                )
            except Exception as e:
                err = e
            if include_raw:
                return {"raw": msg, "parsed": parsed, "parsing_error": err}
            if err is not None:
                raise err
            return parsed

        return bound | RunnableLambda(_parse)

    # ── Options / MCP construction ───────────────────────────

    def _translate_mcp_servers(self) -> dict[str, Any]:
        """ChatAnthropic API-connector list OR CLI-style dict -> CLI dict."""
        if not self.mcp_servers:
            return {}
        if isinstance(self.mcp_servers, dict):
            return dict(self.mcp_servers)
        out: dict[str, Any] = {}
        for i, server in enumerate(self.mcp_servers):
            name = server.get("name", f"server{i}")
            cfg: dict[str, Any] = {"type": "http", "url": server.get("url", "")}
            if server.get("authorization_token"):
                cfg["headers"] = {
                    "Authorization": f"Bearer {server['authorization_token']}"
                }
            out[name] = cfg
        return out

    def _build_sdk_tools(
        self, schemas: list[dict[str, Any]], delivery: ConvertedHistory
    ) -> list[Any]:
        from claude_agent_sdk import tool as sdk_tool

        sdk_tools = []
        for schema in schemas:
            name = schema["name"]

            def _make(name: str = name) -> Any:
                async def handler(args: dict[str, Any]) -> dict[str, Any]:
                    # Idempotent delivery (spike S1b: may be invoked twice)
                    key = (name, canonical_args(args))
                    text = delivery.tool_results_by_key.get(
                        key, delivery.tool_results_by_name.get(name, "")
                    )
                    return {"content": [{"type": "text", "text": text}]}

                return handler

            sdk_tools.append(
                sdk_tool(
                    name,
                    schema.get("description", ""),
                    schema.get("input_schema", {"type": "object", "properties": {}}),
                )(_make())
            )
        return sdk_tools

    def _build_defer_hooks(self, delivery: ConvertedHistory) -> dict[str, Any]:
        from claude_agent_sdk import HookMatcher

        async def defer_hook(
            input_data: dict[str, Any], tool_use_id: str | None, context: Any
        ) -> dict[str, Any]:
            name = strip_tool_namespace(str(input_data.get("tool_name", "")))
            args = input_data.get("tool_input", {})
            key = (name, canonical_args(args))
            # Re-fired pending call with a stored result -> allow (handler delivers)
            if (
                key in delivery.tool_results_by_key
                or name in delivery.tool_results_by_name
            ):
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "defer",
                }
            }

        return {
            "PreToolUse": [
                HookMatcher(
                    matcher=f"{MCP_TOOL_PREFIX}.*", hooks=[cast(Any, defer_hook)]
                )
            ]
        }

    def _build_options(
        self,
        *,
        system: str | None,
        resume: str | None,
        tool_schemas: list[dict[str, Any]] | None,
        delivery: ConvertedHistory,
        server_builtin_tools: list[str] | None = None,
        output_format: dict[str, Any] | None = None,
        include_partial_messages: bool = False,
    ) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

        agentic = self.builtin_tools is not None
        tools: Any
        if self.builtin_tools == "claude_code":
            tools = {"type": "preset", "preset": "claude_code"}
        elif isinstance(self.builtin_tools, list):
            tools = normalize_tools(self.builtin_tools)
        else:
            tools = []
        if server_builtin_tools and isinstance(tools, list):
            tools = normalize_tools([*tools, *server_builtin_tools])

        system_parts = [p for p in (self.system_prompt, system) if p]
        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt="\n\n".join(system_parts) or None,
            tools=tools,
            max_turns=self.max_turns if self.max_turns else (None if agentic else 2),
            include_partial_messages=include_partial_messages,
        )
        options.mcp_servers = self._translate_mcp_servers()

        allowed = normalize_tools(self.allowed_tools) if self.allowed_tools else []
        if tool_schemas:
            sdk_tools = self._build_sdk_tools(tool_schemas, delivery)
            options.mcp_servers[MCP_SERVER_NAME] = create_sdk_mcp_server(
                name=MCP_SERVER_NAME, version="1.0.0", tools=sdk_tools
            )
            allowed += [add_tool_namespace(s["name"]) for s in tool_schemas]
            options.hooks = cast(Any, self._build_defer_hooks(delivery))
        if allowed:
            options.allowed_tools = allowed
        if self.disallowed_tools:
            options.disallowed_tools = normalize_tools(self.disallowed_tools)

        if resume:
            options.resume = resume
        if self.permission_mode:
            options.permission_mode = self.permission_mode
        if self.cwd:
            options.cwd = self.cwd
        if self.add_dirs:
            options.add_dirs = list(self.add_dirs)
        if self.cli_path:
            options.cli_path = self.cli_path
        if self.env:
            options.env = dict(self.env)
        if self.auth == "oauth":
            # options.env only overrides (never unsets) inherited vars, and an
            # empty string makes the CLI fall back to the OAuth login (S9).
            for var in _AUTH_ENV_VARS:
                options.env.setdefault(var, "")
        if self.thinking:
            options.thinking = cast(Any, self.thinking)
        if self.effort:
            options.effort = self.effort
        if self.betas:
            options.betas = cast(Any, list(self.betas))
        if self.max_budget_usd is not None:
            options.max_budget_usd = self.max_budget_usd
        if self.sandbox:
            options.sandbox = cast(Any, self.sandbox)
        if self.fallback_model:
            options.fallback_model = self.fallback_model
        if output_format:
            options.output_format = output_format
        return options

    # ── Files API materialization (spike S7) ─────────────────

    def _file_resolver(self, file_id: str) -> dict[str, Any] | None:
        """Download a Files API file via the Anthropic API and inline it.

        The API key is used ONLY here — it is never passed to the CLI
        subprocess (see `auth`). Returns None when unresolvable.
        """
        key = (
            self.anthropic_api_key
            if isinstance(self.anthropic_api_key, str) and self.anthropic_api_key
            else os.environ.get("ANTHROPIC_API_KEY")
        )
        if not key or not file_id:
            return None
        try:
            import base64

            import anthropic  # type: ignore[import-not-found]

            client = anthropic.Anthropic(api_key=key)
            meta = client.beta.files.retrieve_metadata(file_id)
            blob = client.beta.files.download(file_id)
            data = blob.read() if hasattr(blob, "read") else bytes(blob)
            media = getattr(meta, "mime_type", None) or "application/octet-stream"
            block_type = "image" if media.startswith("image/") else "document"
            return {
                "type": block_type,
                "source": {
                    "type": "base64",
                    "media_type": media,
                    "data": base64.b64encode(data).decode(),
                },
            }
        except Exception:
            return None

    # ── Session / prompt resolution ──────────────────────────

    def _resolve_session(
        self, messages: list[BaseMessage], config: RunnableConfig | None
    ) -> Resolution:
        explicit = None
        thread_id = None
        if config:
            configurable = config.get("configurable") or {}
            explicit = configurable.get("session_id")
            thread_id = configurable.get("thread_id")
        explicit = explicit or self.session_id
        if explicit:
            # Caller manages history: send only the last message as suffix.
            return Resolution(
                strategy="resume", session_id=explicit, suffix=messages[-1:]
            )
        return self._session_cache.resolve(messages, thread_id=thread_id)

    @staticmethod
    def _thread_id(config: RunnableConfig | None) -> str | None:
        if not config:
            return None
        return (config.get("configurable") or {}).get("thread_id")

    def _build_prompt_entries(
        self, resolution: Resolution, converted: ConvertedHistory
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
                "history_mode='replay': the full history is replayed with role "
                "fidelity; each historical user message triggers a live "
                "generation (cost grows with history length).",
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

    # ── Core async run (group 4) ─────────────────────────────

    async def _arun_query(
        self,
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

            async def _collect2() -> None:
                stream = query(prompt=_stream_entries(), options=options)
                try:
                    async for msg in stream:
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
            except TimeoutError as e:
                raise ClaudeCliTimeoutError(
                    f"run exceeded timeout={self.default_request_timeout}s"
                ) from e
            except Exception as e:
                text = str(e)
                if "maximum budget" in text.lower():
                    # Deliberate, user-set limit — terminal, never retried.
                    raise ClaudeCliBudgetExceededError(text) from e
                if "returned an error result" in text:
                    # Explicit CLI error result (not a transport failure):
                    # retrying would just repeat the same failing run.
                    raise
                last_error = e  # transport/process error: retry
                if attempt + 1 < attempts:
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
        self,
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
                thread_id=self._thread_id(kwargs.get("config")),
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
        self,
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
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return _run_sync(self._agenerate(messages, stop, None, **kwargs))

    # ── Streaming (group 8) ──────────────────────────────────

    async def _astream(
        self,
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
                    thread_id=self._thread_id(kwargs.get("config")),
                )

    def _stream(
        self,
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
