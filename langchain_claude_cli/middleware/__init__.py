"""Middleware for LangChain 1.x agents. Requires the `langchain` package."""

from langchain_claude_cli.middleware.claude_code import ClaudeCodeToolsMiddleware

__all__ = ["ClaudeCodeToolsMiddleware"]
