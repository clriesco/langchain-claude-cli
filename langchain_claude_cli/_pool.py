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
import contextlib
import logging
import threading
import time
import weakref
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from langchain_claude_cli.exceptions import ClaudeCliStartupError

logger = logging.getLogger("langchain_claude_cli")


def _atexit_close(ref: weakref.ReferenceType[ClientPool]) -> None:
    """Interpreter-shutdown hook that does not keep the pool alive.

    Registering the bound method instead would make the atexit registry hold a
    strong reference to every pool ever built, so a pool whose owner was
    dropped could never be collected.
    """
    pool = ref()
    if pool is not None:
        # Best-effort at interpreter exit: the process is going away anyway, so
        # never let a wedged subprocess hold up shutdown the way the caller-
        # facing default would.
        pool.close(timeout=2.0)


@dataclass
class _Entry:
    client: Any  # ClaudeSDKClient
    sig: str  # options signature: reuse only when config matches
    last_used: float


class ClientPool:
    """LRU+TTL pool of live ClaudeSDKClient instances, keyed by session_id.

    One pool owns one background event-loop thread and up to ``max_clients``
    live ``claude`` subprocesses. Both are released by :meth:`close` /
    :meth:`aclose`; until then they survive the pool's owner, because the loop
    thread holds a reference back to the pool.
    """

    def __init__(self, max_clients: int = 4, ttl: float = 300.0) -> None:
        self._max = max_clients
        self._ttl = ttl
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._last_session: str | None = None
        self._loop_error: BaseException | None = None
        self._closed = False
        atexit.register(_atexit_close, weakref.ref(self))

    # ── background loop ──────────────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._closed:
                raise ClaudeCliStartupError(
                    "client pool is closed (aclose() was called); build a new "
                    "ChatClaudeCli to use persistent=True again"
                )
            if self._loop is None:

                def _run() -> None:
                    try:
                        loop = asyncio.new_event_loop()
                    except OSError as exc:
                        # A new loop needs file descriptors (selector + self
                        # pipe). Under FD exhaustion this raises here, in the
                        # thread — the waiter below must not be left to time
                        # out into a bare assert.
                        self._loop_error = exc
                        self._loop_ready.set()
                        return
                    self._loop = loop
                    asyncio.set_event_loop(loop)
                    self._loop_ready.set()
                    try:
                        loop.run_forever()
                    finally:
                        with contextlib.suppress(Exception):
                            loop.close()

                self._loop_error = None
                self._thread = threading.Thread(
                    target=_run, daemon=True, name="claude-cli-pool"
                )
                self._thread.start()
        ready = self._loop_ready.wait(timeout=10)
        loop = self._loop
        if loop is None:
            # Was: `assert self._loop is not None`. A bare assert here surfaced
            # as an AssertionError with no message — indistinguishable from a
            # bug in the caller — precisely when the machine was out of file
            # descriptors and the loop thread had died starting up.
            reason = (
                str(self._loop_error)
                if self._loop_error is not None
                else (
                    "thread did not become ready within 10s"
                    if not ready
                    else "loop thread exited during startup"
                )
            )
            raise ClaudeCliStartupError(
                f"persistent client pool could not start its event loop: {reason}"
            ) from self._loop_error
        return loop

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
                logger.debug("pool: evict %s (sig/ttl)", session_id[:8])
                self._evict_locked(session_id)
                return False
            logger.debug("pool: hit %s", session_id[:8])
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

            async def _stream() -> Any:
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

    def has(self, session_id: str | None) -> bool:
        target = self.resolve_target(session_id)
        return bool(target) and target in self._entries

    def resolve_target(self, session_id: str | None) -> str | None:
        """The session an untargeted operation applies to (default: last used)."""
        return session_id or self._last_session

    def interrupt(self, session_id: str | None = None) -> None:
        """Cancel the active run of a pooled session (default: last active)."""
        target = self.resolve_target(session_id)
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

    def close(self, timeout: float = 10.0) -> None:
        """Disconnect every pooled client and stop the background loop thread.

        Idempotent and safe from any thread (including atexit). Blocks until
        the ``claude`` subprocesses are gone, so the file descriptors they held
        are actually back before this returns — bounded by ``timeout``, after
        which shutdown proceeds and the straggler is logged.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = list(self._entries.values())
            self._entries.clear()
            self._last_session = None
            loop, thread = self._loop, self._thread
            self._loop = None
            self._thread = None
            self._loop_ready.clear()

        if loop is not None and not loop.is_closed():
            futures = []
            for entry in entries:
                with contextlib.suppress(Exception):
                    futures.append(
                        asyncio.run_coroutine_threadsafe(
                            entry.client.disconnect(), loop
                        )
                    )
            for fut in futures:
                try:
                    fut.result(timeout=timeout)
                except Exception as exc:
                    logger.debug("pool: disconnect on close failed: %s", exc)
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.debug("pool: loop thread did not exit within %ss", timeout)

    async def aclose(self, timeout: float = 10.0) -> None:
        """Async :meth:`close` — runs the blocking shutdown off the caller's loop."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self.close(timeout))

    def __len__(self) -> int:
        return len(self._entries)
