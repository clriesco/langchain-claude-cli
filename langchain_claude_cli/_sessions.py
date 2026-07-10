"""Session prefix-cache: map LangChain history prefixes to CLI session ids.

BaseChatModel is stateless (full history on every invoke) while the CLI is a
stateful session. The cache lets a growing conversation resume its CLI session
sending only the new suffix (design D4). Fingerprints are content-based,
ignore volatile metadata, and are stable across processes — which is what
makes the persistent FileStore backend (v0.2 D1) work: a new process can
resume conversations started by a previous one.

Two extra recovery paths (v0.2):
- FileStore: fingerprints survive process restarts.
- thread_id (LangGraph checkpointer id): registered alongside each session so
  a trimmed history whose prefix no longer matches can still find its session.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage


def _message_digest(msg: BaseMessage) -> str:
    """Stable digest of one message: role + normalized content (+ tool linkage)."""
    payload: dict = {"role": msg.type}
    content = msg.content
    if isinstance(content, str):
        payload["content"] = content
    else:
        norm = []
        for item in content:
            if isinstance(item, str):
                norm.append({"type": "text", "text": item})
            elif isinstance(item, dict):
                # volatile keys (signatures, indexes) excluded
                norm.append(
                    {
                        k: v
                        for k, v in item.items()
                        if k
                        in ("type", "text", "thinking", "source", "name", "input", "id")
                    }
                )
        payload["content"] = norm
    if isinstance(msg, AIMessage) and msg.tool_calls:
        payload["tool_calls"] = [
            {"name": tc["name"], "args": tc["args"], "id": tc["id"]}
            for tc in msg.tool_calls
        ]
    if isinstance(msg, ToolMessage):
        payload["tool_call_id"] = msg.tool_call_id
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def prefix_fingerprints(messages: list[BaseMessage]) -> list[str]:
    """Rolling fingerprints: fp[k] covers messages[0..k] inclusive. O(n)."""
    fps: list[str] = []
    acc = hashlib.sha256()
    for msg in messages:
        acc.update(_message_digest(msg).encode())
        fps.append(acc.copy().hexdigest())
    return fps


# ── Store backends ───────────────────────────────────────────


class SessionStoreBackend(Protocol):
    """Minimal persistence protocol for the cache (D1). Values are JSON-safe."""

    def get(self, key: str) -> dict | None: ...

    def set(self, key: str, value: dict) -> None: ...

    def keys(self) -> list[str]: ...

    def delete(self, key: str) -> None: ...


class InMemoryStore:
    """Default backend: plain dict, process-local (v0.1 behavior)."""

    def __init__(self) -> None:
        self._data: OrderedDict[str, dict] = OrderedDict()

    def get(self, key: str) -> dict | None:
        return self._data.get(key)

    def set(self, key: str, value: dict) -> None:
        self._data[key] = value
        self._data.move_to_end(key)

    def keys(self) -> list[str]:
        return list(self._data.keys())

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class FileStore:
    """JSON-on-disk backend with POSIX file locking and atomic writes.

    Worst case under concurrent writers is losing a recently-registered
    entry, which degrades that conversation to flatten — never corruption.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(
            path or Path.home() / ".langchain-claude-cli" / "sessions.json"
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _read_all(self) -> dict[str, dict]:
        try:
            with self._path.open() as f:
                self._flock(f, exclusive=False)
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_all(self, data: dict[str, dict]) -> None:
        tmp = self._path.with_suffix(".tmp")
        lock_path = self._path.with_suffix(".lock")
        with lock_path.open("w") as lf:
            self._flock(lf, exclusive=True)
            tmp.write_text(json.dumps(data))
            tmp.replace(self._path)

    @staticmethod
    def _flock(f, *, exclusive: bool) -> None:
        try:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except (ImportError, OSError):  # non-POSIX: atomic rename still protects
            pass

    def get(self, key: str) -> dict | None:
        with self._lock:
            return self._read_all().get(key)

    def set(self, key: str, value: dict) -> None:
        with self._lock:
            data = self._read_all()
            data[key] = value
            self._write_all(data)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._read_all().keys())

    def delete(self, key: str) -> None:
        with self._lock:
            data = self._read_all()
            if key in data:
                del data[key]
                self._write_all(data)


def make_store(spec: str | SessionStoreBackend | None) -> SessionStoreBackend:
    if spec is None or spec == "memory":
        return InMemoryStore()
    if spec == "file":
        return FileStore()
    if isinstance(spec, str):
        raise ValueError(f"Unknown session_store {spec!r}; use 'memory', 'file' or an instance")
    return spec


# ── Resolution ───────────────────────────────────────────────


@dataclass
class Resolution:
    """How to execute this invoke against the CLI."""

    strategy: Literal["new", "resume"]
    session_id: str | None = None
    # messages not covered by the resumed session (empty on full-history match)
    suffix: list[BaseMessage] = field(default_factory=list)


_FP = "fp:"
_THREAD = "thread:"
_ORDER = "__order__"  # LRU bookkeeping key inside the store


class SessionCache:
    """Thread-safe LRU mapping history-prefix fingerprints to CLI session ids."""

    def __init__(
        self,
        maxsize: int = 256,
        store: SessionStoreBackend | None = None,
    ) -> None:
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._store = store or InMemoryStore()

    # LRU order is kept as a JSON list under a reserved key so FileStore
    # eviction survives restarts too.
    def _touch(self, key: str) -> None:
        order = (self._store.get(_ORDER) or {}).get("keys", [])
        if key in order:
            order.remove(key)
        order.append(key)
        while len(order) > self._maxsize:
            evicted = order.pop(0)
            self._store.delete(evicted)
        self._store.set(_ORDER, {"keys": order})

    def register(
        self,
        messages: list[BaseMessage],
        session_id: str,
        thread_id: str | None = None,
    ) -> None:
        if not messages or not session_id:
            return
        fp_key = _FP + prefix_fingerprints(messages)[-1]
        with self._lock:
            self._store.set(fp_key, {"session_id": session_id})
            self._touch(fp_key)
            if thread_id:
                tkey = _THREAD + thread_id
                self._store.set(
                    tkey, {"session_id": session_id, "count": len(messages)}
                )
                self._touch(tkey)

    def resolve(
        self, messages: list[BaseMessage], thread_id: str | None = None
    ) -> Resolution:
        """Longest known prefix wins; thread_id is the fallback recovery path."""
        fps = prefix_fingerprints(messages)
        with self._lock:
            for k in range(len(fps) - 1, -1, -1):
                entry = self._store.get(_FP + fps[k])
                if entry is not None:
                    self._touch(_FP + fps[k])
                    return Resolution(
                        strategy="resume",
                        session_id=entry["session_id"],
                        suffix=messages[k + 1 :],
                    )
            if thread_id:
                entry = self._store.get(_THREAD + thread_id)
                # Only usable if we can determine the new suffix: the session
                # already contains `count` messages of this thread (D2).
                if entry is not None and len(messages) > entry.get("count", 0):
                    self._touch(_THREAD + thread_id)
                    return Resolution(
                        strategy="resume",
                        session_id=entry["session_id"],
                        suffix=messages[entry["count"] :],
                    )
        return Resolution(strategy="new", suffix=list(messages))
