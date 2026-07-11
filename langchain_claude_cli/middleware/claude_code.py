"""ClaudeCodeToolsMiddleware — Claude Code capabilities for any create_agent.

The inverse of langchain-anthropic's middleware: instead of reimplementing
bash/editor/memory tools client-side, this registers ONE tool that delegates
the task to a sandboxed Claude Code agentic run (native permissions, sandbox
and budget). The orchestrating model can be any provider; each delegated run
consumes YOUR Claude subscription quota.
"""

from __future__ import annotations

from typing import Any, Literal, cast

try:
    from langchain.agents.middleware.types import AgentMiddleware
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "ClaudeCodeToolsMiddleware requires the `langchain` package (>=1.0). "
        "Install with: pip install langchain"
    ) from e

from langchain_core.tools import StructuredTool

from langchain_claude_cli.chat_models import ChatClaudeCli
from langchain_claude_cli.exceptions import ClaudeCliError
from langchain_claude_cli.tools import ClaudeTool

_DEFAULT_DESCRIPTION = (
    "Delegate a task to Claude Code, a coding agent with sandboxed access to "
    "the local workspace (read/edit files, run commands, search). Describe "
    "the task in natural language; it returns the final result as text. Use "
    "it for anything requiring filesystem or shell access."
)


class ClaudeCodeToolsMiddleware(AgentMiddleware):
    """Expose Claude Code's native tools to a LangChain agent as one tool.

    Example:
        from langchain.agents import create_agent
        from langchain_claude_cli.middleware import ClaudeCodeToolsMiddleware

        agent = create_agent(
            model=any_chat_model,
            tools=[my_other_tools],
            middleware=[ClaudeCodeToolsMiddleware(cwd="/workspace")],
        )
    """

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-5",
        builtin_tools: list[str | ClaudeTool] | Literal["claude_code"] = "claude_code",
        cwd: str | None = None,
        permission_mode: str = "bypassPermissions",
        sandbox: dict[str, Any] | None = None,
        max_budget_usd: float | None = None,
        max_turns: int | None = None,
        timeout: float = 600.0,
        tool_name: str = "claude_code",
        description: str = _DEFAULT_DESCRIPTION,
    ) -> None:
        super().__init__()
        self._runner = ChatClaudeCli(
            model=model,
            builtin_tools=builtin_tools,
            cwd=cwd,
            permission_mode=cast(Any, permission_mode),
            sandbox=sandbox,
            max_budget_usd=max_budget_usd,
            max_turns=max_turns,
            # Always bounded: if the CLI dies mid-run (e.g. rate-limit kill)
            # the SDK stream can hang instead of raising — a tool that never
            # returns would freeze the whole agent graph.
            timeout=timeout,
            system_prompt=(
                "You are executing one delegated task. Files mentioned in the "
                "task live in your working directory: locate them yourself "
                "(Glob/Grep) instead of asking for paths, do the work, and "
                "reply with the final result only."
            ),
        )

        def _run(task: str) -> str:
            """Run one delegated agentic task; errors become tool results."""
            try:
                result = self._runner.invoke(task)
            except (ClaudeCliError, TimeoutError) as e:
                # Budget/API errors must not break the agent graph (spec):
                # surface them as the tool's result so the model can react.
                return f"claude_code failed: {e}"
            content = result.content
            if isinstance(content, str):
                return content
            return "".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )

        self.tools = [
            StructuredTool.from_function(_run, name=tool_name, description=description)
        ]
