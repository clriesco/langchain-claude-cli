"""Watchdog + logging tests (v0.3 group 2) — no CLI required."""

import asyncio
import logging

import pytest

from langchain_claude_cli import ChatClaudeCli, ClaudeCliTimeoutError


def _hanging_query_factory(closed: list):
    """query() double: emits nothing and hangs until cancelled/closed."""

    def fake_query(*, prompt, options):
        async def gen():
            try:
                if not isinstance(prompt, str):
                    async for _ in prompt:
                        pass
                await asyncio.Event().wait()  # hang forever
                yield  # pragma: no cover
            finally:
                closed.append(True)

        return gen()

    return fake_query


def test_watchdog_aborts_hung_stream(monkeypatch):
    import claude_agent_sdk

    closed: list = []
    monkeypatch.setattr(claude_agent_sdk, "query", _hanging_query_factory(closed))
    llm = ChatClaudeCli(model="claude-haiku-4-5", inactivity_timeout=0.2, max_retries=0)
    with pytest.raises(ClaudeCliTimeoutError, match="no SDK activity"):
        llm.invoke("hi")
    assert closed, "stream was not closed (orphan risk)"


def test_watchdog_defaults():
    assert ChatClaudeCli()._effective_inactivity() == 120.0
    assert ChatClaudeCli(builtin_tools=["Read"])._effective_inactivity() is None
    assert ChatClaudeCli(inactivity_timeout=None)._effective_inactivity() is None
    assert (
        ChatClaudeCli(
            builtin_tools=["Read"], inactivity_timeout=30.0
        )._effective_inactivity()
        == 30.0
    )


def test_watchdog_not_retried(monkeypatch):
    import claude_agent_sdk

    calls: list = []

    def counting_hang(*, prompt, options):
        async def gen():
            calls.append(1)
            if not isinstance(prompt, str):
                async for _ in prompt:
                    pass
            await asyncio.Event().wait()
            yield  # pragma: no cover

        return gen()

    monkeypatch.setattr(claude_agent_sdk, "query", counting_hang)
    llm = ChatClaudeCli(model="claude-haiku-4-5", inactivity_timeout=0.2, max_retries=2)
    with pytest.raises(ClaudeCliTimeoutError):
        llm.invoke("hi")
    assert len(calls) == 1  # a hung CLI is not worth re-running blindly


def test_session_resolution_logged(cassette, caplog):
    llm = ChatClaudeCli(model="claude-haiku-4-5")
    with caplog.at_level(logging.DEBUG, logger="langchain_claude_cli"):
        llm.invoke("Reply with exactly: PONG")
    assert any("session: new" in r.message for r in caplog.records)


def test_watchdog_warning_logged(monkeypatch, caplog):
    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", _hanging_query_factory([]))
    llm = ChatClaudeCli(model="claude-haiku-4-5", inactivity_timeout=0.2, max_retries=0)
    with caplog.at_level(logging.WARNING, logger="langchain_claude_cli"):
        with pytest.raises(ClaudeCliTimeoutError):
            llm.invoke("hi")
    assert any("watchdog" in r.message for r in caplog.records)
