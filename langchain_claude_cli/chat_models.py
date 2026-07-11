"""ChatClaudeCli — ChatAnthropic drop-in backed by the Claude Code CLI.

Uses your Claude Pro/Max subscription via claude-agent-sdk; no API key.
Parity policy (design D6): native where the SDK supports it, client-side
workaround where it doesn't, accepted-no-op (with a one-shot warning) where
the CLI cannot express it. The constructor never raises for a parameter that
ChatAnthropic accepts.

Implementation lives in focused modules (v0.4 split): `_options.py` (SDK
options/MCP/guard), `_runner.py` (execution/retries/watchdog/sessions),
`_streaming.py` (event translation). This module holds the public class:
fields, binding and structured output.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from langchain_claude_cli._compat import warn_once
from langchain_claude_cli._options import (
    _is_server_tool,
    _lc_tool_to_anthropic,
    _OptionsMixin,
)
from langchain_claude_cli._pool import ClientPool
from langchain_claude_cli._runner import (
    _apply_stop_and_max_tokens,  # noqa: F401 — re-exported for tests/back-compat
    _run_sync,  # noqa: F401 — re-exported for back-compat
    _RunnerMixin,
)
from langchain_claude_cli._sessions import SessionCache, make_store
from langchain_claude_cli._streaming import _StreamingMixin
from langchain_claude_cli.exceptions import (
    ClaudeCliBudgetExceededError,  # noqa: F401 — historical import location
    ClaudeCliError,
)
from langchain_claude_cli.tools import ClaudeTool

logger = logging.getLogger("langchain_claude_cli")


class ChatClaudeCli(_OptionsMixin, _RunnerMixin, _StreamingMixin, BaseChatModel):
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
    flatten. "replay" (EXPERIMENTAL): replay unknown histories as multi-message
    input — role fidelity is race-dependent and each historical user message
    triggers a live generation."""
    session_store: Any = "memory"
    """Prefix-cache backend: "memory" (default), "file" (persistent JSON in
    ~/.langchain-claude-cli/, conversations survive restarts) or a
    SessionStoreBackend instance."""
    inactivity_timeout: float | Literal["auto"] | None = "auto"
    """Abort the run if the SDK stream stays silent this long (a dead CLI can
    leave the stream open forever — v0.2 finding). "auto": 120s in pure-LLM
    mode (spike S10: worst observed inter-message gap ~26s), disabled in
    agentic mode (slow tools produce legitimate silence). None disables."""
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

    # ── Tool binding ─────────────────────────────────────────

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

    # ── Structured output ────────────────────────────────────

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
