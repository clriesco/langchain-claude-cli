"""Persistent ClaudeSDKClient pool (v0.2 D4).

A ClaudeSDKClient is bound to the event loop it was created on, while
BaseChatModel entrypoints run on short-lived loops (sync `_generate` spawns
one per invoke). The pool therefore owns a dedicated background event loop
thread; all client operations are marshalled onto it with
run_coroutine_threadsafe, so any caller loop (or none) can use the pool.

Scope (design D4 adjustment): plain conversation turns only — tool-calling
cycles keep the stateless query() path whose defer semantics are validated.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any


@dataclass
class _Entry:
    client: Any  # ClaudeSDKClient
    sig: str  # options signature: reuse only when config matches
    last_used: float


class ClientPool:
    """LRU+TTL pool of live ClaudeSDKClient instances, keyed by session_id."""

    def __init__(self, max_clients: int = 4, ttl: float = 300.0) -> None:
        self._max = max_clients
        self._ttl = ttl
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._last_session: str | None = None
        atexit.register(self.close)

    # ── background loop ──────────────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:

                def _run() -> None:
                    loop = asyncio.new_event_loop()
                    self._loop = loop
                    asyncio.set_event_loop(loop)
                    self._loop_ready.set()
                    loop.run_forever()

                threading.Thread(
                    target=_run, daemon=True, name="claude-cli-pool"
                ).start()
        self._loop_ready.wait(timeout=10)
        assert self._loop is not None
        return self._loop

    def _submit(self, coro: Any) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())

    # ── pool operations (called from any thread/loop) ────────

    def get_for(self, session_id: str, sig: str) -> bool:
        """True if a live, signature-matching client exists for the session."""
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None:
                return False
            if entry.sig != sig or (time.time() - entry.last_used) > self._ttl:
                self._evict_locked(session_id)
                return False
            return True

    async def run_turn(
        self, session_id: str, sig: str, entries: list[dict]
    ) -> tuple[list[Any], Any, dict | None] | None:
        """Run one conversation turn on the pooled client (from caller's loop).

        Returns (assistant_msgs, result, rate_limit) or None to signal the
        caller to fall back to the stateless path.
        """
        if not self.get_for(session_id, sig):
            return None
        with self._lock:
            entry = self._entries[session_id]
            entry.last_used = time.time()
            self._entries.move_to_end(session_id)
            self._last_session = session_id

        async def _turn() -> tuple[list[Any], Any, dict | None]:
            from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage

            async def _stream():
                for e in entries:
                    yield e

            await entry.client.query(_stream())
            msgs: list[Any] = []
            result: Any = None
            rate: dict | None = None
            async for msg in entry.client.receive_response():
                if isinstance(msg, AssistantMessage):
                    msgs.append(msg)
                elif isinstance(msg, ResultMessage):
                    result = msg
                elif isinstance(msg, RateLimitEvent):
                    from langchain_claude_cli._convert import rate_limit_to_meta

                    rate = rate_limit_to_meta(msg)
            return msgs, result, rate

        try:
            return await asyncio.wrap_future(self._submit(_turn()))
        except Exception:
            # Broken client: drop it and let the caller fall back.
            self.evict(session_id)
            return None

    def warm(self, session_id: str, options: Any, sig: str) -> None:
        """Fire-and-forget: connect a client resuming `session_id` for reuse."""
        if session_id in self._entries:
            return

        async def _connect() -> None:
            from claude_agent_sdk import ClaudeSDKClient

            client = ClaudeSDKClient(options)
            try:
                await client.connect()
            except Exception:
                return
            with self._lock:
                if session_id in self._entries:
                    fut = asyncio.ensure_future(client.disconnect())
                    del fut
                    return
                self._entries[session_id] = _Entry(client, sig, time.time())
                self._entries.move_to_end(session_id)
                self._last_session = session_id
                while len(self._entries) > self._max:
                    oldest = next(iter(self._entries))
                    self._evict_locked(oldest)

        self._submit(_connect())

    def interrupt(self, session_id: str | None = None) -> None:
        """Cancel the active run of a pooled session (default: last active)."""
        target = session_id or self._last_session
        if not target:
            return
        with self._lock:
            entry = self._entries.get(target)
        if entry is None:
            return
        self._submit(entry.client.interrupt()).result(timeout=30)

    def set_model(self, model: str | None, session_id: str | None = None) -> None:
        target = session_id or self._last_session
        if not target:
            return
        with self._lock:
            entry = self._entries.get(target)
        if entry is None:
            return
        self._submit(entry.client.set_model(model)).result(timeout=30)

    # ── eviction / shutdown ──────────────────────────────────

    def _evict_locked(self, session_id: str) -> None:
        entry = self._entries.pop(session_id, None)
        if entry is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(entry.client.disconnect(), self._loop)

    def evict(self, session_id: str) -> None:
        with self._lock:
            self._evict_locked(session_id)

    def close(self) -> None:
        with self._lock:
            sessions = list(self._entries)
        for sid in sessions:
            self.evict(sid)

    def __len__(self) -> int:
        return len(self._entries)
