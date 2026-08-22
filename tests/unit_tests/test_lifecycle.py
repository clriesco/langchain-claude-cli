"""Releasing the persistent client pool (point 4) and its startup failure mode.

`persistent=True` builds one pool *per ChatClaudeCli instance* — one background
loop thread and up to `pool_max_clients` live `claude` subprocesses each. A
process that builds an instance per unit of work (the natural pattern when the
prompt differs per unit) multiplies both, so the instance has to be closable.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from langchain_claude_cli import ChatClaudeCli, ClaudeCliStartupError
from langchain_claude_cli._pool import ClientPool, _Entry


def _pool_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "claude-cli-pool"]


class _FakeClient:
    def __init__(self) -> None:
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


# ── the pool releases its own resources ──────────────────────


def test_close_stops_the_background_loop_thread():
    before = len(_pool_threads())
    pool = ClientPool()
    pool._ensure_loop()
    assert len(_pool_threads()) == before + 1
    pool.close()
    assert len(_pool_threads()) == before, "loop thread outlived close()"


def test_close_disconnects_pooled_clients():
    pool = ClientPool()
    pool._ensure_loop()
    client = _FakeClient()
    pool._entries["s1"] = _Entry(client, "sig", 0.0)
    pool.close()
    assert client.disconnected, "close() returned before the subprocess was gone"
    assert len(pool) == 0


def test_close_is_idempotent():
    pool = ClientPool()
    pool._ensure_loop()
    pool.close()
    pool.close()  # must not raise


def test_closed_pool_refuses_to_restart_silently():
    pool = ClientPool()
    pool.close()
    with pytest.raises(ClaudeCliStartupError, match="closed"):
        pool._ensure_loop()


def test_aclose_releases_the_thread_from_async_code():
    before = len(_pool_threads())

    async def main():
        pool = ClientPool()
        pool._ensure_loop()
        await pool.aclose()

    asyncio.run(main())
    assert len(_pool_threads()) == before


# ── ChatClaudeCli exposes the lifecycle ──────────────────────


def test_chat_model_close_releases_the_pool():
    before = len(_pool_threads())
    llm = ChatClaudeCli(model="claude-haiku-4-5", persistent=True)
    llm._pool._ensure_loop()
    assert len(_pool_threads()) == before + 1
    llm.close()
    assert llm._pool is None
    assert len(_pool_threads()) == before


def test_chat_model_aclose_releases_the_pool():
    before = len(_pool_threads())

    async def main():
        llm = ChatClaudeCli(model="claude-haiku-4-5", persistent=True)
        llm._pool._ensure_loop()
        await llm.aclose()
        assert llm._pool is None

    asyncio.run(main())
    assert len(_pool_threads()) == before


def test_sync_context_manager_closes_the_pool():
    before = len(_pool_threads())
    with ChatClaudeCli(model="claude-haiku-4-5", persistent=True) as llm:
        llm._pool._ensure_loop()
        assert len(_pool_threads()) == before + 1
    assert len(_pool_threads()) == before


def test_async_context_manager_closes_the_pool():
    before = len(_pool_threads())

    async def main():
        async with ChatClaudeCli(model="claude-haiku-4-5", persistent=True) as llm:
            llm._pool._ensure_loop()
            assert len(_pool_threads()) == before + 1

    asyncio.run(main())
    assert len(_pool_threads()) == before


def test_close_is_a_noop_without_persistent():
    llm = ChatClaudeCli(model="claude-haiku-4-5")
    assert llm._pool is None
    llm.close()
    llm.close()


def test_many_instances_release_every_thread():
    """50 instances built and closed leave nothing behind (the measured shape)."""
    before = len(_pool_threads())
    models = [
        ChatClaudeCli(model="claude-haiku-4-5", persistent=True) for _ in range(10)
    ]
    for m in models:
        m._pool._ensure_loop()
    assert len(_pool_threads()) == before + 10
    for m in models:
        m.close()
    assert len(_pool_threads()) == before


# ── bonus: the messageless AssertionError ────────────────────


def test_loop_startup_failure_raises_typed_error_not_bare_assert(monkeypatch):
    """Out of descriptors, the loop thread dies before it can set the loop.

    This used to fall through to `assert self._loop is not None` — an
    AssertionError with no message, indistinguishable from a bug in the caller.
    """
    real = asyncio.new_event_loop

    def boom():
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(asyncio, "new_event_loop", boom)
    pool = ClientPool()
    try:
        with pytest.raises(ClaudeCliStartupError) as info:
            pool._ensure_loop()
    finally:
        monkeypatch.setattr(asyncio, "new_event_loop", real)
    assert "Too many open files" in str(info.value)
    assert str(info.value), "the error must carry a message"
    assert isinstance(info.value.__cause__, OSError)
