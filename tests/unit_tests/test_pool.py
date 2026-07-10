"""Unit tests for the persistent client pool (fake clients, no CLI)."""

import asyncio
import time

from langchain_claude_cli import ChatClaudeCli
from langchain_claude_cli._pool import ClientPool, _Entry


class FakeClient:
    def __init__(self):
        self.disconnected = False
        self.interrupted = False

    async def disconnect(self):
        self.disconnected = True

    async def interrupt(self):
        self.interrupted = True

    async def set_model(self, model):
        self.model = model


def _put(pool: ClientPool, sid: str, sig: str = "s") -> FakeClient:
    client = FakeClient()
    pool._ensure_loop()
    with pool._lock:
        pool._entries[sid] = _Entry(client, sig, time.time())
        pool._last_session = sid
    return client


def test_get_for_matching_signature():
    pool = ClientPool()
    _put(pool, "a", sig="X")
    assert pool.get_for("a", "X")
    assert not pool.get_for("missing", "X")


def test_signature_mismatch_evicts():
    pool = ClientPool()
    client = _put(pool, "a", sig="X")
    assert not pool.get_for("a", "OTHER")
    assert len(pool) == 0
    time.sleep(0.2)
    assert client.disconnected


def test_ttl_expiry_evicts():
    pool = ClientPool(ttl=0.01)
    _put(pool, "a")
    time.sleep(0.05)
    assert not pool.get_for("a", "s")
    assert len(pool) == 0


def test_interrupt_targets_last_session():
    pool = ClientPool()
    client = _put(pool, "a")
    pool.interrupt()
    assert client.interrupted


def test_close_disconnects_all():
    pool = ClientPool()
    c1, c2 = _put(pool, "a"), _put(pool, "b")
    pool.close()
    time.sleep(0.2)
    assert len(pool) == 0
    assert c1.disconnected and c2.disconnected


def test_run_turn_falls_back_when_absent():
    pool = ClientPool()

    async def main():
        return await pool.run_turn("nope", "s", [{"type": "user"}])

    assert asyncio.run(main()) is None


def test_default_model_has_no_pool():
    llm = ChatClaudeCli()
    assert llm._pool is None


def test_interrupt_requires_persistent():
    import pytest

    from langchain_claude_cli import ClaudeCliError

    llm = ChatClaudeCli()
    with pytest.raises(ClaudeCliError, match="persistent"):
        llm.interrupt()
