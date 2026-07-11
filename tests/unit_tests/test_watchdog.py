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


def test_stateless_interrupt(monkeypatch):
    """v0.4: interrupt() cancels an active stateless run from another thread."""
    import threading
    import time

    import claude_agent_sdk

    from langchain_claude_cli import ClaudeCliInterruptedError

    closed: list = []
    monkeypatch.setattr(claude_agent_sdk, "query", _hanging_query_factory(closed))
    llm = ChatClaudeCli(
        model="claude-haiku-4-5", inactivity_timeout=None, max_retries=0
    )
    errors: list = []

    def _invoke():
        try:
            llm.invoke("hi")
        except BaseException as e:
            errors.append(e)

    t = threading.Thread(target=_invoke)
    t.start()
    deadline = time.time() + 5
    while not llm._active_runs and time.time() < deadline:
        time.sleep(0.05)
    assert llm._active_runs, "run never registered"
    llm.interrupt()
    t.join(timeout=10)
    assert not t.is_alive(), "interrupt did not end the invoke"
    assert errors and isinstance(errors[0], ClaudeCliInterruptedError), errors
    assert closed, "stream was not closed (orphan risk)"
    assert not llm._active_runs, "run not unregistered"


def test_external_cancellation_not_masked(monkeypatch):
    """A cancel NOT triggered by interrupt() must stay a CancelledError."""
    import asyncio

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", _hanging_query_factory([]))
    llm = ChatClaudeCli(model="claude-haiku-4-5", inactivity_timeout=None)

    async def main():
        task = asyncio.create_task(
            llm._agenerate(
                [
                    __import__(
                        "langchain_core.messages", fromlist=["HumanMessage"]
                    ).HumanMessage(content="hi")
                ]
            )
        )
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "wrong"

    assert asyncio.run(main()) == "cancelled"
