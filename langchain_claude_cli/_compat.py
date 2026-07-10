"""ChatAnthropic signature-compatibility layer: no-op params and one-shot warnings."""

from __future__ import annotations

import warnings


class ClaudeCliCompatWarning(UserWarning):
    """A ChatAnthropic parameter was accepted but has no effect via the Claude CLI."""


_warned: set[str] = set()


def warn_once(param: str, message: str) -> None:
    """Emit a ClaudeCliCompatWarning once per process for a given parameter."""
    if param in _warned:
        return
    _warned.add(param)
    warnings.warn(message, ClaudeCliCompatWarning, stacklevel=3)
