"""Session prefix-cache: map LangChain history prefixes to CLI session ids.

BaseChatModel is stateless (full history on every invoke) while the CLI is a
stateful session. The cache lets a growing conversation resume its CLI session
sending only the new suffix (design D4). Fingerprints are content-based and
ignore volatile metadata, so the AIMessage we returned last turn fingerprints
identically when the caller sends it back.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

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


@dataclass
class Resolution:
    """How to execute this invoke against the CLI."""

    strategy: Literal["new", "resume"]
    session_id: str | None = None
    # messages not covered by the resumed session (empty on full-history match)
    suffix: list[BaseMessage] = field(default_factory=list)


class SessionCache:
    """Thread-safe LRU mapping history-prefix fingerprints to CLI session ids."""

    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._data: OrderedDict[str, str] = OrderedDict()

    def register(self, messages: list[BaseMessage], session_id: str) -> None:
        if not messages or not session_id:
            return
        fp = prefix_fingerprints(messages)[-1]
        with self._lock:
            self._data[fp] = session_id
            self._data.move_to_end(fp)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def resolve(self, messages: list[BaseMessage]) -> Resolution:
        """Longest known prefix wins; no known prefix -> new session."""
        fps = prefix_fingerprints(messages)
        with self._lock:
            for k in range(len(fps) - 1, -1, -1):
                session_id = self._data.get(fps[k])
                if session_id is not None:
                    self._data.move_to_end(fps[k])
                    return Resolution(
                        strategy="resume",
                        session_id=session_id,
                        suffix=messages[k + 1 :],
                    )
        return Resolution(strategy="new", suffix=list(messages))
