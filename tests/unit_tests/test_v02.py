"""Unit tests for v0.2 features: typed errors, OAuth guard, rate limit, Files API."""

import warnings
from dataclasses import dataclass

import pytest
from langchain_core.messages import HumanMessage

import langchain_claude_cli._compat as compat
from langchain_claude_cli import (
    ChatClaudeCli,
    ClaudeCliAuthError,
    ClaudeCliError,
    ClaudeCliOverloadedError,
    ClaudeCliRateLimitError,
)
from langchain_claude_cli._convert import (
    ConvertedHistory,
    convert_lc_messages,
    rate_limit_to_meta,
)
from langchain_claude_cli.exceptions import classify_status


@pytest.fixture(autouse=True)
def _reset_warned():
    compat._warned.clear()


# ── 4.5 typed exceptions ─────────────────────────────────────


def test_classify_status_taxonomy():
    assert isinstance(classify_status(429, "x"), ClaudeCliRateLimitError)
    assert isinstance(classify_status(529, "x"), ClaudeCliOverloadedError)
    assert isinstance(classify_status(503, "x"), ClaudeCliOverloadedError)
    assert isinstance(classify_status(401, "x"), ClaudeCliAuthError)
    assert isinstance(classify_status(403, "x"), ClaudeCliAuthError)
    assert isinstance(
        classify_status(None, "Invalid API key. Please run /login"), ClaudeCliAuthError
    )
    generic = classify_status(None, "something else")
    assert isinstance(generic, ClaudeCliError)
    assert not isinstance(generic, ClaudeCliAuthError)


def test_exceptions_are_runtimeerrors():
    assert issubclass(ClaudeCliRateLimitError, RuntimeError)
    from langchain_claude_cli import ClaudeCliTimeoutError

    assert issubclass(ClaudeCliTimeoutError, TimeoutError)


# ── 4.6 OAuth guard ──────────────────────────────────────────


def _build_opts(llm: ChatClaudeCli):
    return llm._build_options(
        system=None, resume=None, tool_schemas=None, delivery=ConvertedHistory()
    )


def test_auth_oauth_neutralizes_inherited_keys():
    opts = _build_opts(ChatClaudeCli())
    assert opts.env["ANTHROPIC_API_KEY"] == ""
    assert opts.env["ANTHROPIC_AUTH_TOKEN"] == ""


def test_auth_inherit_leaves_env_alone():
    opts = _build_opts(ChatClaudeCli(auth="inherit"))
    assert "ANTHROPIC_API_KEY" not in opts.env


def test_auth_oauth_respects_explicit_user_env():
    opts = _build_opts(ChatClaudeCli(env={"ANTHROPIC_API_KEY": "sk-user-explicit"}))
    assert opts.env["ANTHROPIC_API_KEY"] == "sk-user-explicit"


# ── 4.1 rate limit mapping ───────────────────────────────────


@dataclass
class FakeInfo:
    status: str = "allowed"
    rate_limit_type: str = "five_hour"
    utilization: float | None = 0.42
    resets_at: int = 1783727400


@dataclass
class FakeEvent:
    rate_limit_info: FakeInfo


def test_rate_limit_to_meta():
    meta = rate_limit_to_meta(FakeEvent(FakeInfo()))
    assert meta == {
        "status": "allowed",
        "type": "five_hour",
        "utilization": 0.42,
        "resets_at": 1783727400,
    }


# ── 4.2 Files API blocks ─────────────────────────────────────

FILE_BLOCK = {
    "type": "document",
    "source": {"type": "file", "file_id": "file-abc"},
}


def test_file_block_dropped_with_warning_without_resolver():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = convert_lc_messages(
            [HumanMessage(content=[FILE_BLOCK, {"type": "text", "text": "hi"}])]
        )
    blocks = out.entries[0]["message"]["content"]
    assert blocks == [{"type": "text", "text": "hi"}]
    assert any("Files API" in str(w.message) for w in caught)


def test_file_block_materialized_via_resolver():
    def resolver(file_id: str):
        assert file_id == "file-abc"
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": "QQ==",
            },
        }

    out = convert_lc_messages(
        [HumanMessage(content=[FILE_BLOCK])], file_resolver=resolver
    )
    block = out.entries[0]["message"]["content"][0]
    assert block["source"]["type"] == "base64"


def test_base64_document_blocks_unaffected():
    inline = {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": "QQ=="},
    }
    out = convert_lc_messages([HumanMessage(content=[inline])])
    assert out.entries[0]["message"]["content"][0] == inline


# ── Regression: retryable error with no attempts left must raise ──
# https://github.com/clriesco/langchain-claude-cli — downstream report:
# "Retryable API error results are silently returned as empty AIMessages"


def _error_result(status: int):
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=True,
        num_turns=1,
        session_id="sess-err",
        api_error_status=status,
    )


def _fake_query_factory(status: int, calls: list):
    def fake_query(*, prompt, options):
        async def gen():
            calls.append(1)
            # drain the prompt stream like the real SDK does
            async for _ in prompt:
                pass
            yield _error_result(status)

        return gen()

    return fake_query


def test_retryable_error_raises_with_max_retries_zero(monkeypatch):
    import claude_agent_sdk

    calls: list = []
    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query_factory(429, calls))
    llm = ChatClaudeCli(model="claude-haiku-4-5", max_retries=0)
    with pytest.raises(ClaudeCliRateLimitError):
        llm.invoke("hi")
    assert len(calls) == 1  # no retries with max_retries=0


def test_retryable_error_raises_after_exhausting_retries(monkeypatch):
    import claude_agent_sdk

    calls: list = []
    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query_factory(529, calls))
    llm = ChatClaudeCli(model="claude-haiku-4-5", max_retries=1)
    with pytest.raises(ClaudeCliOverloadedError):
        llm.invoke("hi")
    assert len(calls) == 2  # initial + 1 retry, then raise (not silent return)
