"""Typed exception taxonomy for fallback policies (rate_limit/overloaded/auth/timeout).

Downstream retry/fallback logic can catch these instead of classifying error
text. All inherit from ClaudeCliError (a RuntimeError).
"""

from __future__ import annotations


class ClaudeCliError(RuntimeError):
    """Base class for langchain-claude-cli errors."""


class ClaudeCliBudgetExceededError(ClaudeCliError):
    """The run stopped because it reached the configured max_budget_usd."""


class ClaudeCliRateLimitError(ClaudeCliError):
    """The API rejected the run due to rate limiting (HTTP 429)."""


class ClaudeCliOverloadedError(ClaudeCliError):
    """The API is overloaded or failing upstream (HTTP 5xx / 529)."""


class ClaudeCliAuthError(ClaudeCliError):
    """Authentication with the CLI/API failed (HTTP 401/403 or login problem)."""


class ClaudeCliTimeoutError(ClaudeCliError, TimeoutError):
    """The run exceeded the configured timeout."""


class ClaudeCliInterruptedError(ClaudeCliError):
    """The run was cancelled via interrupt()."""


_AUTH_MARKERS = (
    "authentication",
    "unauthorized",
    "invalid api key",
    "api key",
    "please run /login",
    "log in",
    "oauth",
)


def classify_status(status: int | None, detail: str) -> ClaudeCliError:
    """Map an api_error_status (and error text) to a typed exception."""
    if status == 429:
        return ClaudeCliRateLimitError(f"rate limited (429): {detail}")
    if status in (401, 403):
        return ClaudeCliAuthError(f"auth error ({status}): {detail}")
    if status is not None and status >= 500:
        return ClaudeCliOverloadedError(
            f"API overloaded/unavailable ({status}): {detail}"
        )
    lowered = detail.lower()
    if any(m in lowered for m in _AUTH_MARKERS):
        return ClaudeCliAuthError(detail)
    return ClaudeCliError(f"Claude CLI run failed: {detail}")
