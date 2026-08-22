"""langchain-claude-cli — ChatAnthropic drop-in backed by the Claude Code CLI."""

import logging as _logging

from langchain_claude_cli._compat import ClaudeCliCompatWarning
from langchain_claude_cli._sessions import (
    FileStore,
    InMemoryStore,
    SessionStoreBackend,
)
from langchain_claude_cli.chat_models import ChatClaudeCli
from langchain_claude_cli.exceptions import (
    ClaudeCliAuthError,
    ClaudeCliBudgetExceededError,
    ClaudeCliError,
    ClaudeCliExecutionError,
    ClaudeCliInterruptedError,
    ClaudeCliMaxTurnsError,
    ClaudeCliNotFoundError,
    ClaudeCliOverloadedError,
    ClaudeCliProcessError,
    ClaudeCliRateLimitError,
    ClaudeCliResultError,
    ClaudeCliStartupError,
    ClaudeCliTimeoutError,
    ClaudeCliTransportError,
)
from langchain_claude_cli.tools import (
    ALL_TOOLS,
    NETWORK_TOOLS,
    READ_ONLY_TOOLS,
    SHELL_TOOLS,
    WRITE_TOOLS,
    ClaudeTool,
)

_logging.getLogger("langchain_claude_cli").addHandler(_logging.NullHandler())

__all__ = [
    "ALL_TOOLS",
    "NETWORK_TOOLS",
    "READ_ONLY_TOOLS",
    "SHELL_TOOLS",
    "WRITE_TOOLS",
    "ChatClaudeCli",
    "ClaudeCliAuthError",
    "ClaudeCliBudgetExceededError",
    "ClaudeCliCompatWarning",
    "ClaudeCliError",
    "ClaudeCliExecutionError",
    "ClaudeCliInterruptedError",
    "ClaudeCliMaxTurnsError",
    "ClaudeCliNotFoundError",
    "ClaudeCliOverloadedError",
    "ClaudeCliProcessError",
    "ClaudeCliRateLimitError",
    "ClaudeCliResultError",
    "ClaudeCliStartupError",
    "ClaudeCliTimeoutError",
    "ClaudeCliTransportError",
    "ClaudeTool",
    "FileStore",
    "InMemoryStore",
    "SessionStoreBackend",
]
