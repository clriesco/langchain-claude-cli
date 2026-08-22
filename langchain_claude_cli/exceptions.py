"""Typed exception taxonomy for fallback policies (rate_limit/overloaded/auth/timeout).

Downstream retry/fallback logic can catch these instead of classifying error
text. All inherit from ClaudeCliError (a RuntimeError).

Two properties this module commits to:

* **Every** error raised out of this package is a ``ClaudeCliError``. Errors
  originating in ``claude_agent_sdk`` are re-raised as subclasses that also
  inherit from the original SDK class, so ``except ClaudeCliError`` and
  ``except CLIConnectionError`` both keep working (1.1.0).
* An error carries what the run already knew. When the CLI produced a
  ``ResultMessage`` before failing, its cost/usage/turn counters are attached
  to the exception (:meth:`ClaudeCliError.attach_result`) — a failed run can
  still be accounted for.
"""

from __future__ import annotations

import re
from typing import Any

from claude_agent_sdk import (
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
)
from claude_agent_sdk._errors import ClaudeSDKError
from langchain_core.messages.ai import UsageMetadata

from langchain_claude_cli._convert import usage_to_usage_metadata


class ClaudeCliError(RuntimeError):
    """Base class for langchain-claude-cli errors.

    Since 1.1.0 an instance may carry the accounting data of the run that
    failed. The attributes default to ``None`` and are populated by
    :meth:`attach_result` whenever the CLI emitted a ``ResultMessage`` before
    the failure — which it does for every error that is not a startup or
    transport failure.

    Attributes:
        result_message: the raw ``ResultMessage``, or None.
        usage: token usage **as the CLI reports it**, or None. Anthropic's
            convention: ``input_tokens`` counts only the uncached tokens, with
            ``cache_read_input_tokens`` and ``cache_creation_input_tokens``
            alongside it. Prefer ``usage_metadata`` unless you specifically
            want the CLI's own shape.
        usage_metadata: the same usage in LangChain's shape (1.1.1) — the one
            ``AIMessage.usage_metadata`` uses on the success path, so a token
            counter can add successes and failures without reconciling two
            conventions. ``None`` when ``usage`` is.
        total_cost_usd: cost of the failed run in USD, or None.
        num_turns: agentic turns consumed by the failed run, or None.
        duration_ms: wall-clock duration of the failed run, or None.
        session_id: CLI session the failed run belonged to, or None.
        subtype: the ``ResultMessage`` subtype (e.g. ``"error_max_turns"``).

    Note:
        ``usage["input_tokens"]`` and ``usage_metadata["input_tokens"]`` are
        both present and mean **different things**: the first excludes cached
        tokens, the second is the sum of uncached + cache_read +
        cache_creation. Mixing them in one counter silently undercounts, and
        failed runs — which chain the most turns and read the most cache — are
        where the gap is widest. Pick one shape and stay in it.
    """

    # Class-level defaults: reading them is always safe, and adding them here
    # (rather than to __init__) keeps every existing constructor call valid.
    result_message: Any = None
    usage: dict[str, Any] | None = None
    usage_metadata: UsageMetadata | None = None
    total_cost_usd: float | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    session_id: str | None = None
    subtype: str | None = None

    def attach_result(self, result: Any) -> ClaudeCliError:
        """Copy the accounting fields off a ``ResultMessage``. Returns self.

        Designed to be chained at the raise site::

            raise classify_status(status, detail).attach_result(result)

        A ``None`` result is a no-op, so callers never need to guard.
        """
        if result is None:
            return self
        self.result_message = result
        for attr in (
            "usage",
            "total_cost_usd",
            "num_turns",
            "duration_ms",
            "session_id",
            "subtype",
        ):
            value = getattr(result, attr, None)
            if value is not None:
                setattr(self, attr, value)
        # Converted here rather than left to the caller: the two shapes share
        # the key `input_tokens` with different meanings, so every consumer
        # doing it by hand is a chance to sum the wrong one.
        self.usage_metadata = usage_to_usage_metadata(self.usage)
        return self

    def __reduce__(self) -> tuple[Any, ...]:
        """Preserve attached attributes across pickling (multiprocessing pools).

        ``BaseException.__reduce__`` returns ``(cls, args)`` only, which would
        silently drop everything ``attach_result`` set.
        """
        return (self.__class__, self.args, self.__dict__.copy())


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


# ── CLI error results (1.1.0) ────────────────────────────────
#
# The CLI reports these by emitting a ResultMessage with is_error=true and a
# descriptive subtype, then exiting non-zero on purpose. The SDK turns that
# exit into a bare `Exception` whose only distinguishing feature is its prose
# ("Claude Code returned an error result: ..."). These types replace that
# prose with the structured subtype the CLI already sent.


class ClaudeCliResultError(ClaudeCliError):
    """The CLI completed the run but reported it as failed.

    Not a transport failure: the request reached the model and consumed
    budget, so retrying repeats the same run. ``total_cost_usd`` and friends
    are always populated when the ResultMessage was captured.
    """


class ClaudeCliMaxTurnsError(ClaudeCliResultError):
    """The agentic loop hit ``max_turns`` before finishing (``error_max_turns``).

    ``num_turns`` holds the turns consumed, read from the ResultMessage when
    available and parsed out of the CLI's message otherwise.
    """


class ClaudeCliExecutionError(ClaudeCliResultError):
    """The CLI failed part-way through the run (``error_during_execution``)."""


# ── SDK errors, wrapped into this package's hierarchy (1.1.0) ─
#
# Each of these inherits from BOTH ClaudeCliError and the SDK class it
# replaces, so `except ClaudeCliError` sees it (the point) without breaking
# any existing `except CLIConnectionError` / `except ProcessError`.


class ClaudeCliTransportError(ClaudeCliError, ClaudeSDKError):
    """An error from claude_agent_sdk that is not a startup failure."""


class ClaudeCliProcessError(ClaudeCliTransportError, ProcessError):
    """The CLI subprocess failed (SDK ``ProcessError``)."""

    def __init__(
        self, message: str, exit_code: int | None = None, stderr: str | None = None
    ) -> None:
        # NOT ProcessError.__init__: it appends "(exit code: N)" and the stderr
        # to the message, which would duplicate both when re-typing an error
        # whose message the SDK already formatted. Set the attributes it
        # exposes and keep `message` byte-for-byte.
        self.exit_code = exit_code
        self.stderr = stderr
        Exception.__init__(self, message)


class ClaudeCliStartupError(ClaudeCliError, CLIConnectionError):
    """The CLI subprocess could not be started or connected to.

    Infrastructure, not data: the run never reached the model, so nothing was
    consumed and the work is safe to requeue. File-descriptor exhaustion
    (``[Errno 24] Too many open files``) and a dead pool loop arrive here.
    """


class ClaudeCliNotFoundError(ClaudeCliStartupError, CLINotFoundError):
    """The ``claude`` executable was not found (SDK ``CLINotFoundError``)."""


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


# Fallback only: used when the ResultMessage was not captured and the subtype
# is therefore unavailable. The structured `result.subtype` is always
# preferred — see classify_result_error.
_MAX_TURNS_TEXT = re.compile(r"reached maximum number of turns\s*\((\d+)\)", re.I)


def classify_result_error(result: Any, text: str) -> ClaudeCliError:
    """Type a CLI *error result* from its subtype, falling back to its text.

    ``text`` is the SDK's message and is preserved verbatim as the exception
    message, so consumers still matching on that prose keep working while they
    migrate to the types.
    """
    subtype = getattr(result, "subtype", None)
    num_turns = getattr(result, "num_turns", None)
    match = _MAX_TURNS_TEXT.search(text)

    exc: ClaudeCliError
    if subtype == "error_max_turns" or (subtype is None and match):
        exc = ClaudeCliMaxTurnsError(text)
        if num_turns is None and match:
            num_turns = int(match.group(1))
        exc.attach_result(result)
        # attach_result may not have found num_turns on the result.
        if exc.num_turns is None:
            exc.num_turns = num_turns
        return exc
    if subtype == "error_during_execution":
        exc = ClaudeCliExecutionError(text)
    else:
        exc = ClaudeCliResultError(text)
    return exc.attach_result(result)


# Mapping is ordered most-specific first; CLINotFoundError is a subclass of
# CLIConnectionError, and ProcessError/CLIConnectionError of ClaudeSDKError.
_SDK_WRAPPERS: tuple[tuple[type[BaseException], type[ClaudeCliError]], ...] = (
    (CLINotFoundError, ClaudeCliNotFoundError),
    (CLIConnectionError, ClaudeCliStartupError),
    (ProcessError, ClaudeCliProcessError),
    (ClaudeSDKError, ClaudeCliTransportError),
)


def wrap_sdk_error(exc: BaseException) -> BaseException:
    """Re-type an SDK exception into this package's hierarchy.

    Anything that is already a ``ClaudeCliError`` (or not an SDK error at all)
    is returned untouched, so this is safe to apply at any boundary. The
    original is preserved as ``__cause__`` and as ``.sdk_error``.
    """
    if isinstance(exc, ClaudeCliError) or not isinstance(exc, ClaudeSDKError):
        return exc
    for sdk_cls, wrapper_cls in _SDK_WRAPPERS:
        if isinstance(exc, sdk_cls):
            if wrapper_cls is ClaudeCliProcessError:
                wrapped: ClaudeCliError = ClaudeCliProcessError(
                    str(exc),
                    getattr(exc, "exit_code", None),
                    getattr(exc, "stderr", None),
                )
            else:
                wrapped = wrapper_cls(str(exc))
            wrapped.sdk_error = exc  # type: ignore[attr-defined]
            wrapped.__cause__ = exc
            return wrapped
    return exc  # pragma: no cover - ClaudeSDKError catch-all above is total
