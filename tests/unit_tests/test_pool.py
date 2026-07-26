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


def test_interrupt_with_no_active_run_raises():
    """v0.4: interrupt() works in any mode, but needs something to cancel."""
    import pytest

    from langchain_claude_cli import ClaudeCliError

    llm = ChatClaudeCli()
    with pytest.raises(ClaudeCliError, match="no active run"):
        llm.interrupt()


# ── 1.0.0: an interrupted session must not be resumed ────────

SESSION = "22222222-2222-2222-2222-222222222222"


class _FakeLoop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class _FakeTask:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def _active_token(llm: ChatClaudeCli, session_id: str | None):
    from types import SimpleNamespace

    token = SimpleNamespace(
        loop=_FakeLoop(), task=_FakeTask(), session_id=session_id, interrupted=False
    )
    llm._active_runs[id(token)] = token
    return token


def test_interrupt_invalidates_the_interrupted_session():
    """Resuming a session cancelled mid-generation replays the abandoned
    answer instead of the next message, hijacking the conversation."""
    from langchain_core.messages import AIMessage, HumanMessage

    history = [HumanMessage(content="hola"), AIMessage(content="¡hola!")]
    llm = ChatClaudeCli(model="haiku")
    llm._session_cache.register(history, SESSION)
    assert llm._session_cache.resolve(history).session_id == SESSION

    token = _active_token(llm, SESSION)
    llm.interrupt()

    assert token.interrupted and token.task.cancelled
    # Next turn opens a fresh session from the caller's history.
    assert llm._session_cache.resolve(history).strategy == "new"


def test_interrupt_leaves_other_conversations_alone():
    from langchain_core.messages import AIMessage, HumanMessage

    mine = [HumanMessage(content="a"), AIMessage(content="b")]
    other = [HumanMessage(content="x"), AIMessage(content="y")]
    other_session = "33333333-3333-3333-3333-333333333333"

    llm = ChatClaudeCli(model="haiku")
    llm._session_cache.register(mine, SESSION)
    llm._session_cache.register(other, other_session)

    _active_token(llm, SESSION)
    llm.interrupt(session_id=SESSION)

    assert llm._session_cache.resolve(mine).strategy == "new"
    assert llm._session_cache.resolve(other).session_id == other_session
