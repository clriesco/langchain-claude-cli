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
        self.clients: list[dict] = []
        self.index = 0
        self.client_index = 0
        if not RECORDING:
            if not path.exists():
                raise FileNotFoundError(
                    f"Cassette {path.name} not recorded yet. Run:\n"
                    f"  RECORD_CASSETTES=1 pytest <test> "
                )
            data = json.loads(path.read_text())
            if isinstance(data, list):  # legacy: query-only cassette
                self.exchanges = data
            else:
                self.exchanges = data.get("query", [])
                self.clients = data.get("clients", [])

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

    # ── ClaudeSDKClient double (v0.4 D2: pool coverage) ─────

    def make_client(self, options: Any) -> Any:
        if RECORDING:
            return _RecordingClient(self, options)
        if self.client_index >= len(self.clients):
            raise AssertionError(
                f"Cassette {self.path.name}: client #{self.client_index + 1} "
                f"requested but only {len(self.clients)} recorded"
            )
        record = self.clients[self.client_index]
        self.client_index += 1
        assert getattr(options, "model", None) == record["meta"]["model"]
        assert bool(getattr(options, "resume", None)) == record["meta"]["resume"]
        return _ReplayClient(record)

    def save(self) -> None:
        if not RECORDING or not (self.exchanges or self.clients):
            return
        CASSETTE_DIR.mkdir(exist_ok=True)
        if self.clients:
            payload: Any = {"query": self.exchanges, "clients": self.clients}
        else:
            payload = self.exchanges  # keep legacy shape for query-only tests
        self.path.write_text(json.dumps(payload, indent=1))


class _ReplayClient:
    """Replays one recorded ClaudeSDKClient's turns."""

    def __init__(self, record: dict) -> None:
        self._turns = list(record["turns"])
        self._current: dict | None = None
        self.disconnected = False

    async def connect(self) -> None:
        pass

    async def query(self, prompt: Any) -> None:
        n = 0
        if not isinstance(prompt, str):
            async for _ in prompt:
                n += 1
        assert self._turns, "replay client: no turns left"
        self._current = self._turns.pop(0)
        assert self._current["entries"] == n, (
            f"replay client: {n} entries sent, {self._current['entries']} recorded"
        )

    def receive_response(self) -> Any:
        turn = self._current

        async def gen():
            for message in turn["messages"]:
                yield from_jsonable(message)

        return gen()

    async def interrupt(self) -> None:
        pass

    async def set_model(self, model: Any = None) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected = True


class _RecordingClient:
    """Wraps the real ClaudeSDKClient, teeing turns into the cassette."""

    def __init__(self, player: CassettePlayer, options: Any) -> None:
        real_cls = _REAL_CLIENT_CLS
        self._real = real_cls(options)
        self._record: dict = {
            "meta": {
                "model": getattr(options, "model", None),
                "resume": bool(getattr(options, "resume", None)),
            },
            "turns": [],
        }
        player.clients.append(self._record)

    async def connect(self) -> None:
        await self._real.connect()

    async def query(self, prompt: Any) -> None:
        entries = 0
        if isinstance(prompt, str):
            await self._real.query(prompt)
        else:

            async def tee():
                nonlocal entries
                async for e in prompt:
                    entries += 1
                    yield e

            await self._real.query(tee())
        self._record["turns"].append({"entries": entries, "messages": []})

    def receive_response(self) -> Any:
        turn = self._record["turns"][-1]

        async def gen():
            async for msg in self._real.receive_response():
                if type(msg).__name__ in _RECORDED_TYPES:
                    turn["messages"].append(to_jsonable(msg))
                yield msg

        return gen()

    async def interrupt(self) -> None:
        await self._real.interrupt()

    async def set_model(self, model: Any = None) -> None:
        await self._real.set_model(model)

    async def disconnect(self) -> None:
        await self._real.disconnect()


_REAL_QUERY = claude_agent_sdk.query
_REAL_CLIENT_CLS = claude_agent_sdk.ClaudeSDKClient
