"""Error taxonomy: typed CLI error results, cost on failure, wrapped SDK errors.

The literal strings in this module are the ones measured in production on
2026-08-22 (batch of 50 municipalities, `builtin_tools=NETWORK_TOOLS`,
`max_turns=25`). They are pinned deliberately: the previous generation of this
logic matched on third-party prose and broke silently when the wording changed.
"""

from __future__ import annotations

import pickle

import claude_agent_sdk
import pytest
from claude_agent_sdk import CLIConnectionError, CLINotFoundError, ProcessError
from claude_agent_sdk import ResultMessage as SDKResultMessage

from langchain_claude_cli import (
    ChatClaudeCli,
    ClaudeCliError,
    ClaudeCliExecutionError,
    ClaudeCliMaxTurnsError,
    ClaudeCliNotFoundError,
    ClaudeCliProcessError,
    ClaudeCliRateLimitError,
    ClaudeCliResultError,
    ClaudeCliStartupError,
    ClaudeCliTransportError,
)
from langchain_claude_cli.exceptions import wrap_sdk_error

# ── measured fixtures ────────────────────────────────────────

# What the SDK raises after the CLI reports error_max_turns and exits non-zero
# (claude_agent_sdk/_internal/query.py: the ProcessError is replaced by this).
MAX_TURNS_TEXT = (
    "Claude Code returned an error result: Reached maximum number of turns (25)"
)
# What CLIConnectionError carried when the process ran out of descriptors.
EMFILE_TEXT = "Failed to start Claude Code: [Errno 24] Too many open files"


def _result(
    subtype: str,
    *,
    is_error: bool = True,
    num_turns: int = 25,
    api_error_status: int | None = None,
    errors: list[str] | None = None,
) -> SDKResultMessage:
    """A ResultMessage shaped like the ones the failing runs actually emitted."""
    return SDKResultMessage(
        subtype=subtype,
        duration_ms=356_000,
        duration_api_ms=340_000,
        is_error=is_error,
        num_turns=num_turns,
        session_id="4f0c2b1e-0000-4000-8000-000000000001",
        total_cost_usd=0.3612,
        usage={
            "input_tokens": 1520,
            "output_tokens": 8400,
            "cache_read_input_tokens": 210_000,
        },
        errors=errors,
        api_error_status=api_error_status,
    )


def _failing_query(result: SDKResultMessage | None, exc: BaseException):
    """query() double: emit `result` (if any), then fail like the CLI does."""

    def fake_query(*, prompt, options):
        async def gen():
            if not isinstance(prompt, str):
                async for _ in prompt:
                    pass
            if result is not None:
                yield result
            raise exc

        return gen()

    return fake_query


def _llm(monkeypatch, result, exc, **kwargs) -> ChatClaudeCli:
    monkeypatch.setattr(claude_agent_sdk, "query", _failing_query(result, exc))
    kwargs.setdefault("max_retries", 0)
    return ChatClaudeCli(model="claude-haiku-4-5", **kwargs)


# ── point 2: exhausting the turns has its own type ───────────


def test_max_turns_raises_typed_error(monkeypatch):
    llm = _llm(
        monkeypatch,
        _result("error_max_turns", errors=["Reached maximum number of turns (25)"]),
        Exception(MAX_TURNS_TEXT),
    )
    with pytest.raises(ClaudeCliMaxTurnsError) as info:
        llm.invoke("hi")
    exc = info.value
    assert exc.num_turns == 25
    assert isinstance(exc, ClaudeCliResultError)
    assert isinstance(exc, ClaudeCliError)


def test_max_turns_keeps_the_cli_prose_in_its_message(monkeypatch):
    """Consumers still matching on the text must not break while they migrate."""
    llm = _llm(
        monkeypatch,
        _result("error_max_turns"),
        Exception(MAX_TURNS_TEXT),
    )
    with pytest.raises(ClaudeCliMaxTurnsError) as info:
        llm.invoke("hi")
    assert str(info.value) == MAX_TURNS_TEXT


def test_max_turns_typed_from_subtype_not_from_wording(monkeypatch):
    """Reworded CLI prose must not change the type — the subtype decides."""
    llm = _llm(
        monkeypatch,
        _result("error_max_turns", num_turns=25),
        Exception("Claude Code returned an error result: turn budget exhausted"),
    )
    with pytest.raises(ClaudeCliMaxTurnsError) as info:
        llm.invoke("hi")
    assert info.value.num_turns == 25


def test_max_turns_falls_back_to_text_when_result_missing(monkeypatch):
    """No ResultMessage captured: parse the turns out of the message instead."""
    llm = _llm(monkeypatch, None, Exception(MAX_TURNS_TEXT))
    with pytest.raises(ClaudeCliMaxTurnsError) as info:
        llm.invoke("hi")
    assert info.value.num_turns == 25
    assert info.value.total_cost_usd is None


def test_error_during_execution_is_a_distinct_type(monkeypatch):
    llm = _llm(
        monkeypatch,
        _result("error_during_execution"),
        Exception("Claude Code returned an error result: error_during_execution"),
    )
    with pytest.raises(ClaudeCliExecutionError) as info:
        llm.invoke("hi")
    assert not isinstance(info.value, ClaudeCliMaxTurnsError)
    assert isinstance(info.value, ClaudeCliResultError)


def test_max_turns_is_terminal_not_retried(monkeypatch):
    calls: list[int] = []

    def counting(*, prompt, options):
        async def gen():
            if not isinstance(prompt, str):
                async for _ in prompt:
                    pass
            calls.append(1)
            yield _result("error_max_turns")
            raise Exception(MAX_TURNS_TEXT)

        return gen()

    monkeypatch.setattr(claude_agent_sdk, "query", counting)
    llm = ChatClaudeCli(model="claude-haiku-4-5", max_retries=3)
    with pytest.raises(ClaudeCliMaxTurnsError):
        llm.invoke("hi")
    assert len(calls) == 1, "a spent turn budget must not be retried"


# ── point 1: a failed run can still be costed ────────────────


def test_max_turns_error_carries_cost_and_usage(monkeypatch):
    llm = _llm(
        monkeypatch,
        _result("error_max_turns"),
        Exception(MAX_TURNS_TEXT),
    )
    with pytest.raises(ClaudeCliMaxTurnsError) as info:
        llm.invoke("hi")
    exc = info.value
    assert exc.total_cost_usd == pytest.approx(0.3612)
    assert exc.usage == {
        "input_tokens": 1520,
        "output_tokens": 8400,
        "cache_read_input_tokens": 210_000,
    }
    assert exc.num_turns == 25
    assert exc.duration_ms == 356_000
    assert exc.session_id == "4f0c2b1e-0000-4000-8000-000000000001"
    assert exc.subtype == "error_max_turns"
    assert exc.result_message is not None
    assert exc.result_message.total_cost_usd == pytest.approx(0.3612)


def test_api_status_error_carries_cost_too(monkeypatch):
    """The HTTP-status path (429/5xx) must not drop the accounting either."""
    result = _result("success", api_error_status=429, errors=["rate limited"])

    def fake_query(*, prompt, options):
        async def gen():
            if not isinstance(prompt, str):
                async for _ in prompt:
                    pass
            yield result

        return gen()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    llm = ChatClaudeCli(model="claude-haiku-4-5", max_retries=0)
    with pytest.raises(ClaudeCliRateLimitError) as info:
        llm.invoke("hi")
    assert info.value.total_cost_usd == pytest.approx(0.3612)
    assert info.value.num_turns == 25


def test_attached_accounting_survives_pickling():
    """Batch runners hand failures across process boundaries."""
    exc = ClaudeCliMaxTurnsError(MAX_TURNS_TEXT).attach_result(
        _result("error_max_turns")
    )
    revived = pickle.loads(pickle.dumps(exc))
    assert isinstance(revived, ClaudeCliMaxTurnsError)
    assert str(revived) == MAX_TURNS_TEXT
    assert revived.total_cost_usd == pytest.approx(0.3612)
    assert revived.num_turns == 25


def test_attach_result_tolerates_none():
    exc = ClaudeCliError("boom")
    assert exc.attach_result(None) is exc
    assert exc.total_cost_usd is None


def test_plain_errors_default_to_none():
    """Reading the accounting off any error is always safe."""
    exc = ClaudeCliError("boom")
    assert exc.usage is None
    assert exc.total_cost_usd is None
    assert exc.num_turns is None
    assert exc.duration_ms is None


# ── point 3: SDK errors arrive inside this package's hierarchy ─


def test_startup_failure_is_a_claude_cli_error(monkeypatch):
    """The measured EMFILE failure: infrastructure, and catchable as ours."""
    llm = _llm(monkeypatch, None, CLIConnectionError(EMFILE_TEXT))
    with pytest.raises(ClaudeCliStartupError) as info:
        llm.invoke("hi")
    exc = info.value
    assert isinstance(exc, ClaudeCliError), "the hole point 3 reported"
    assert isinstance(exc, CLIConnectionError), "existing handlers keep working"
    assert EMFILE_TEXT in str(exc)
    assert isinstance(exc.sdk_error, CLIConnectionError)


def test_startup_failure_caught_by_except_claude_cli_error(monkeypatch):
    """The consumer's requeue branch is written as `except ClaudeCliError`."""
    llm = _llm(monkeypatch, None, CLIConnectionError(EMFILE_TEXT))
    try:
        llm.invoke("hi")
    except ClaudeCliError as exc:
        assert isinstance(exc, ClaudeCliStartupError)
    else:  # pragma: no cover
        pytest.fail("CLIConnectionError escaped the package hierarchy again")


def test_cli_not_found_keeps_its_sdk_type(monkeypatch):
    llm = _llm(monkeypatch, None, CLINotFoundError("Claude Code not found"))
    with pytest.raises(ClaudeCliNotFoundError) as info:
        llm.invoke("hi")
    assert isinstance(info.value, CLINotFoundError)
    assert isinstance(info.value, ClaudeCliStartupError)
    assert isinstance(info.value, ClaudeCliError)


def test_process_error_keeps_exit_code_and_stderr(monkeypatch):
    llm = _llm(monkeypatch, None, ProcessError("crashed", 1, "some stderr"))
    with pytest.raises(ClaudeCliProcessError) as info:
        llm.invoke("hi")
    exc = info.value
    assert isinstance(exc, ProcessError)
    assert isinstance(exc, ClaudeCliError)
    assert exc.exit_code == 1
    assert exc.stderr == "some stderr"


def test_wrap_sdk_error_leaves_our_own_and_foreign_errors_alone():
    ours = ClaudeCliRateLimitError("rl")
    assert wrap_sdk_error(ours) is ours
    foreign = ValueError("not an SDK error")
    assert wrap_sdk_error(foreign) is foreign


def test_startup_error_is_distinguishable_from_a_data_failure():
    """The branch that decides requeue-vs-human-review needs these disjoint."""
    startup = wrap_sdk_error(CLIConnectionError(EMFILE_TEXT))
    result_err = ClaudeCliMaxTurnsError(MAX_TURNS_TEXT)
    assert isinstance(startup, ClaudeCliStartupError)
    assert not isinstance(startup, ClaudeCliResultError)
    assert not isinstance(result_err, ClaudeCliStartupError)


# ── the streaming path types errors the same way ─────────────


def test_stream_raises_typed_max_turns_with_cost(monkeypatch):
    """`.stream()` used to surface the bare SDK Exception and drop the cost."""
    llm = _llm(
        monkeypatch,
        _result("error_max_turns"),
        Exception(MAX_TURNS_TEXT),
    )
    with pytest.raises(ClaudeCliMaxTurnsError) as info:
        list(llm.stream("hi"))
    assert info.value.num_turns == 25
    assert info.value.total_cost_usd == pytest.approx(0.3612)


def test_stream_startup_failure_is_a_claude_cli_error(monkeypatch):
    llm = _llm(monkeypatch, None, CLIConnectionError(EMFILE_TEXT))
    with pytest.raises(ClaudeCliStartupError):
        list(llm.stream("hi"))


# ── the success path is untouched ────────────────────────────


def test_successful_invoke_keeps_its_public_metadata(monkeypatch):
    """The keys production depends on must survive the raise-site changes."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    ok = SDKResultMessage(
        subtype="success",
        duration_ms=1200,
        duration_api_ms=1100,
        is_error=False,
        num_turns=2,
        session_id="4f0c2b1e-0000-4000-8000-000000000002",
        total_cost_usd=0.01,
        usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100},
    )

    def fake_query(*, prompt, options):
        async def gen():
            if not isinstance(prompt, str):
                async for _ in prompt:
                    pass
            yield AssistantMessage(content=[TextBlock(text="hola")], model="m")
            yield ok

        return gen()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    msg = ChatClaudeCli(model="claude-haiku-4-5").invoke("hi")
    assert msg.content == "hola"
    assert msg.response_metadata["num_turns"] == 2
    assert msg.response_metadata["total_cost_usd"] == 0.01
    assert msg.response_metadata["duration_ms"] == 1200
    assert msg.usage_metadata["input_token_details"]["cache_read"] == 100


# ── wrapping must not alter what the error says ──────────────


@pytest.mark.parametrize(
    "original",
    [
        CLIConnectionError(EMFILE_TEXT),
        CLINotFoundError("Claude Code not found"),
        ProcessError("crashed", 1, "some stderr"),
        ProcessError("crashed with no extras"),
    ],
    ids=["connection", "not_found", "process_full", "process_bare"],
)
def test_wrapping_preserves_the_message_verbatim(original):
    """Anything logging or matching str(exc) must see exactly what it saw before."""
    assert str(wrap_sdk_error(original)) == str(original)


# ── usage en las dos formas (1.1.1) ──────────────────────────


def test_error_exposes_usage_in_langchain_shape(monkeypatch):
    """`usage` es el dict del CLI; `usage_metadata` es la forma de LangChain."""
    llm = _llm(monkeypatch, _result("error_max_turns"), Exception(MAX_TURNS_TEXT))
    with pytest.raises(ClaudeCliMaxTurnsError) as info:
        llm.invoke("hi")
    exc = info.value

    # El crudo se conserva intacto: convención de Anthropic, `input_tokens`
    # cuenta solo los NO cacheados.
    assert exc.usage["input_tokens"] == 1520
    assert exc.usage["cache_read_input_tokens"] == 210_000

    # El convertido agrega, que es la convención de LangChain.
    assert exc.usage_metadata == {
        "input_tokens": 211_520,
        "output_tokens": 8400,
        "total_tokens": 219_920,
        "input_token_details": {"cache_read": 210_000, "cache_creation": 0},
    }


def test_failure_usage_matches_the_success_path_shape(monkeypatch):
    """La propiedad que evita el bug: un contador puede sumar ambos caminos.

    Con el mismo `usage`, lo que el fallo expone en `usage_metadata` tiene que
    ser byte a byte lo que un turno correcto pone en `AIMessage.usage_metadata`.
    Si divergen, quien acumule tokens a través de éxitos y fallos suma dos
    convenciones distintas bajo la misma clave `input_tokens`.
    """
    from claude_agent_sdk import AssistantMessage, TextBlock

    usage = {
        "input_tokens": 1520,
        "output_tokens": 8400,
        "cache_read_input_tokens": 210_000,
    }

    ok = SDKResultMessage(
        subtype="success",
        duration_ms=1200,
        duration_api_ms=1100,
        is_error=False,
        num_turns=2,
        session_id="s",
        total_cost_usd=0.01,
        usage=usage,
    )

    def good_query(*, prompt, options):
        async def gen():
            if not isinstance(prompt, str):
                async for _ in prompt:
                    pass
            yield AssistantMessage(content=[TextBlock(text="ok")], model="m")
            yield ok

        return gen()

    monkeypatch.setattr(claude_agent_sdk, "query", good_query)
    exito = ChatClaudeCli(model="claude-haiku-4-5").invoke("hi").usage_metadata

    fallo = ClaudeCliMaxTurnsError(MAX_TURNS_TEXT).attach_result(
        _result("error_max_turns")
    )
    assert fallo.usage_metadata == exito


def test_usage_metadata_is_none_without_usage():
    """Un arranque fallido no gastó nada: ni crudo, ni convertido, ni 0."""
    exc = ClaudeCliStartupError(EMFILE_TEXT)
    assert exc.usage is None
    assert exc.usage_metadata is None


# ── ningún tipo del SDK puede quedarse sin wrapper ───────────


def _sdk_error_classes() -> list[type]:
    """Every concrete error class the SDK defines, discovered at runtime."""
    import inspect

    from claude_agent_sdk import _errors as sdk_errors
    from claude_agent_sdk._errors import ClaudeSDKError

    return [
        cls
        for _, cls in inspect.getmembers(sdk_errors, inspect.isclass)
        if issubclass(cls, ClaudeSDKError) and cls is not ClaudeSDKError
    ]


def _instantiate(cls: type) -> BaseException:
    """One instance per SDK error class, honouring each constructor."""
    # MessageParseError is not re-exported by claude_agent_sdk's top level.
    from claude_agent_sdk._errors import CLIJSONDecodeError, MessageParseError

    if cls is ProcessError:
        return cls("crashed", 1, "some stderr")
    if cls is CLIJSONDecodeError:
        return cls("{malformed", ValueError("boom"))
    if cls is MessageParseError:
        return cls("unparseable", {"type": "weird"})
    return cls("boom")


@pytest.mark.parametrize("sdk_cls", _sdk_error_classes(), ids=lambda c: c.__name__)
def test_every_sdk_error_keeps_its_own_type_when_wrapped(sdk_cls):
    """Discovered dynamically so a NEW SDK error class fails this test.

    A type that falls through to the generic ClaudeCliTransportError stops
    satisfying `except <that type>` downstream — which is how CLIJSONDecodeError
    silently flipped a consumer's retry decision in 1.1.0.
    """
    original = _instantiate(sdk_cls)
    wrapped = wrap_sdk_error(original)

    assert isinstance(wrapped, ClaudeCliError), sdk_cls
    assert isinstance(wrapped, sdk_cls), (
        f"{sdk_cls.__name__} lost its own type: `except {sdk_cls.__name__}` "
        f"would stop catching it (got {type(wrapped).__name__})"
    )
    assert str(wrapped) == str(original), sdk_cls
    assert type(wrapped) is not ClaudeCliTransportError, (
        f"{sdk_cls.__name__} needs its own wrapper in _SDK_WRAPPERS"
    )


def test_json_decode_error_keeps_line_and_cause():
    from claude_agent_sdk import CLIJSONDecodeError

    from langchain_claude_cli import ClaudeCliJSONDecodeError

    cause = ValueError("boom")
    wrapped = wrap_sdk_error(CLIJSONDecodeError("{malformed", cause))
    assert isinstance(wrapped, ClaudeCliJSONDecodeError)
    assert isinstance(wrapped, CLIJSONDecodeError)
    assert wrapped.line == "{malformed"
    assert wrapped.original_error is cause


def test_message_parse_error_keeps_data():
    from claude_agent_sdk._errors import MessageParseError

    from langchain_claude_cli import ClaudeCliMessageParseError

    wrapped = wrap_sdk_error(MessageParseError("unparseable", {"type": "weird"}))
    assert isinstance(wrapped, ClaudeCliMessageParseError)
    assert isinstance(wrapped, MessageParseError)
    assert wrapped.data == {"type": "weird"}
