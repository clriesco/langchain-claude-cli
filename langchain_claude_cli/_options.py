"""Options construction: ClaudeAgentOptions, MCP wiring, defer hooks, OAuth guard.

Split out of chat_models.py in v0.4 (design D1) — pure refactor, no behavior
change. `_OptionsMixin` methods run with ``self`` being the ChatClaudeCli
instance (fields live there); the import is type-checking-only to avoid a
runtime cycle.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from langchain_claude_cli._convert import (
    MCP_SERVER_NAME,
    MCP_TOOL_PREFIX,
    ConvertedHistory,
    add_tool_namespace,
    canonical_args,
    strip_tool_namespace,
)
from langchain_claude_cli.tools import normalize_tools

if TYPE_CHECKING:
    from langchain_claude_cli.chat_models import ChatClaudeCli

logger = logging.getLogger("langchain_claude_cli")

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


class _OptionsMixin:
    """ClaudeAgentOptions construction for ChatClaudeCli."""

    def _options_sig(self: ChatClaudeCli) -> str:
        """Client reuse is only valid when the run configuration matches."""
        import json

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

    def _session_profile(self: ChatClaudeCli) -> str:
        """Digest of the STABLE execution profile, for session-key namespacing.

        A LangGraph `thread_id` identifies a graph thread, not a conversation:
        several model instances routinely share one (e.g. a cheap router and an
        expensive executor). Namespacing the thread key by profile keeps them
        from resuming each other's CLI session.

        Deliberately narrower than `_options_sig()`: `system_prompt` is excluded
        because runtimes recompose it every turn (date, memory, active skills).
        Including it would make the key volatile and disable recovery entirely.
        """
        import hashlib
        import json

        raw = json.dumps(
            [self.model, self.cwd, self.builtin_tools, self.permission_mode],
            default=str,
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _translate_mcp_servers(self: ChatClaudeCli) -> dict[str, Any]:
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
        self: ChatClaudeCli, schemas: list[dict[str, Any]], delivery: ConvertedHistory
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

    def _build_defer_hooks(
        self: ChatClaudeCli, delivery: ConvertedHistory
    ) -> dict[str, Any]:
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
                logger.debug("tools: deliver %s", name)
                return {}
            logger.debug("tools: defer %s", name)
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
        self: ChatClaudeCli,
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

    def _file_resolver(self: ChatClaudeCli, file_id: str) -> dict[str, Any] | None:
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
