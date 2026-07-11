"""Cassette harness: record/replay claude-agent-sdk streams (design D1, v0.3).

Interception point is the ``claude_agent_sdk.query`` boundary — every
non-persistent code path in the library goes through it (imported per-call,
so monkeypatching the module attribute is enough).

- Replay (default): each ``query()`` call consumes the next recorded exchange
  from ``tests/cassettes/<test>.json``; SDK dataclasses are reconstructed so
  the library's isinstance checks see the real types. No CLI, no quota.
- Record: ``RECORD_CASSETTES=1`` delegates to the real query, tees the prompt
  stream and message stream, and dumps them with lax request metadata
  (model, entry count, tools presence) that replay asserts against.

Only the message types the library consumes are recorded (AssistantMessage,
ResultMessage, StreamEvent, RateLimitEvent) — cassettes assert library
behavior, not full CLI fidelity (that's the contract suite's job).
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import claude_agent_sdk
from claude_agent_sdk import types as sdk_types

CASSETTE_DIR = Path(__file__).parent / "cassettes"
RECORDING = os.environ.get("RECORD_CASSETTES") == "1"

_RECORDED_TYPES = (
    "AssistantMessage",
    "ResultMessage",
    "StreamEvent",
    "RateLimitEvent",
)

# All dataclasses in the SDK types module, for reconstruction (blocks included)
_SDK_CLASSES: dict[str, type] = {
    name: obj
    for name, obj in vars(sdk_types).items()
    if isinstance(obj, type) and dataclasses.is_dataclass(obj)
}


def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        payload = {"__type__": type(obj).__name__}
        for f in dataclasses.fields(obj):
            payload[f.name] = to_jsonable(getattr(obj, f.name))
        return payload
    if isinstance(obj, list):
        return [to_jsonable(i) for i in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    return obj


def from_jsonable(obj: Any) -> Any:
    if isinstance(obj, dict) and "__type__" in obj:
        data = dict(obj)
        cls = _SDK_CLASSES[data.pop("__type__")]
        fields = {f.name for f in dataclasses.fields(cls) if f.init}
        return cls(**{k: from_jsonable(v) for k, v in data.items() if k in fields})
    if isinstance(obj, list):
        return [from_jsonable(i) for i in obj]
    if isinstance(obj, dict):
        return {k: from_jsonable(v) for k, v in obj.items()}
    return obj


def _request_meta(options: Any, n_entries: int, prompt_was_str: bool) -> dict:
    return {
        "model": getattr(options, "model", None),
        "n_entries": n_entries,
        "prompt_was_str": prompt_was_str,
        "has_lc_tools": bool("lc" in (getattr(options, "mcp_servers", None) or {})),
        "resume": bool(getattr(options, "resume", None)),
    }


class CassettePlayer:
    """Replays (or records) the sequence of query() exchanges of one test."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.exchanges: list[dict] = []
        self.index = 0
        if not RECORDING:
            if not path.exists():
                raise FileNotFoundError(
                    f"Cassette {path.name} not recorded yet. Run:\n"
                    f"  RECORD_CASSETTES=1 pytest <test> "
                )
            self.exchanges = json.loads(path.read_text())

    # ── replay ───────────────────────────────────────────────

    def replay_query(self, *, prompt: Any, options: Any) -> Any:
        async def gen():
            n = 0
            prompt_was_str = isinstance(prompt, str)
            if not prompt_was_str:
                async for _ in prompt:
                    n += 1
            if self.index >= len(self.exchanges):
                raise AssertionError(
                    f"Cassette {self.path.name}: exchange #{self.index + 1} "
                    f"requested but only {len(self.exchanges)} recorded"
                )
            exchange = self.exchanges[self.index]
            self.index += 1
            recorded = exchange["request"]
            actual = _request_meta(options, n, prompt_was_str)
            for key in (
                "model",
                "n_entries",
                "prompt_was_str",
                "has_lc_tools",
                "resume",
            ):
                assert actual[key] == recorded[key], (
                    f"Cassette {self.path.name} exchange #{self.index}: "
                    f"request mismatch on {key!r}: got {actual[key]!r}, "
                    f"recorded {recorded[key]!r}"
                )
            for message in exchange["messages"]:
                yield from_jsonable(message)

        return gen()

    # ── record ───────────────────────────────────────────────

    def recording_query(self, *, prompt: Any, options: Any) -> Any:
        real_query = _REAL_QUERY

        async def gen():
            entries: list = []
            prompt_was_str = isinstance(prompt, str)

            async def tee():
                async for entry in prompt:
                    entries.append(entry)
                    yield entry

            messages: list = []
            real_prompt = prompt if prompt_was_str else tee()
            try:
                async for msg in real_query(prompt=real_prompt, options=options):
                    if type(msg).__name__ in _RECORDED_TYPES:
                        messages.append(to_jsonable(msg))
                    yield msg
            finally:
                # Consumers may close the stream early (stop_sequences,
                # interrupt): record whatever was streamed up to that point.
                self.exchanges.append(
                    {
                        "request": _request_meta(options, len(entries), prompt_was_str),
                        "messages": messages,
                    }
                )

        return gen()

    def save(self) -> None:
        if RECORDING and self.exchanges:
            CASSETTE_DIR.mkdir(exist_ok=True)
            self.path.write_text(json.dumps(self.exchanges, indent=1))


_REAL_QUERY = claude_agent_sdk.query
