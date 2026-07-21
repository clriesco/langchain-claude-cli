"""Stale-session degrade tests (0.4.3) — no CLI required.

The CLI purges inactive transcripts (`cleanupPeriodDays`), so a persisted
mapping can point to a session that no longer exists. The doubles here mimic
the empirically-verified failure shape: the marker only reaches the SDK's
``options.stderr`` callback, while the ProcessError itself carries a
placeholder (see design D1 of fix-stale-session-resume).
"""

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent
from claude_agent_sdk._errors import ProcessError
from claude_agent_sdk.types import TextBlock
from langchain_core.messages import AIMessage, HumanMessage

from langchain_claude_cli import ChatClaudeCli
from langchain_claude_cli._sessions import SessionCache

STALE_ID = "00000000-dead-beef-0000-000000000000"
NEW_ID = "11111111-1111-1111-1111-111111111111"
MARKER = f"No conversation found with session ID: {STALE_ID}"

HIST = [HumanMessage(content="hola"), AIMessage(content="¡Hola! ¿Qué tal?")]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    )


def _result(session_id: str) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
    )


def _raise_stale(options) -> None:
    """Fail exactly like the real CLI on a purged session (design D1)."""
    stderr_cb = getattr(options, "stderr", None)
    if stderr_cb is not None:
        stderr_cb(MARKER)
    raise ProcessError(
        "Command failed with exit code 1",
        exit_code=1,
        stderr="Check stderr output for details",
    )


def _stale_then_ok_factory(calls: list, fail_new_runs: int = 0):
    """query() double: any resume dies stale; new sessions answer.

    ``fail_new_runs`` makes the first N new-session runs raise a generic
    transport error, to prove the degraded run keeps its full retry budget.
    """
    remaining_failures = [fail_new_runs]

    def fake_query(*, prompt, options):
        async def gen():
            if not isinstance(prompt, str):
                async for _ in prompt:
                    pass
            calls.append(getattr(options, "resume", None))
            if getattr(options, "resume", None):
                _raise_stale(options)
            if remaining_failures[0] > 0:
                remaining_failures[0] -= 1
                raise RuntimeError("transient transport error")
            yield StreamEvent(
                uuid="u1",
                session_id=NEW_ID,
                event={
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "sigo aquí"},
                },
            )
            yield AssistantMessage(content=[TextBlock(text="sigo aquí")], model="haiku")
            yield _result(NEW_ID)

        return gen()

    return fake_query


def _poisoned_llm(monkeypatch, calls: list, **kwargs) -> ChatClaudeCli:
    import claude_agent_sdk

    monkeypatch.setattr(
        claude_agent_sdk,
        "query",
        _stale_then_ok_factory(calls, kwargs.pop("fail_new_runs", 0)),
    )
    kwargs.setdefault("max_retries", 3)
    llm = ChatClaudeCli(model="haiku", **kwargs)
    llm._session_cache.register(HIST, STALE_ID, thread_id="prof:t1")
    return llm


def _store_session_ids(llm: ChatClaudeCli) -> set:
    store = llm._session_cache._store
    keys = store.keys()  # protocol method — the store is not a dict
    return {
        (store.get(key) or {}).get("session_id") for key in keys if key != "__order__"
    }


# ── SessionCache.invalidate ──────────────────────────────────


def test_invalidate_drops_every_mapping_of_the_session():
    cache = SessionCache()
    h1 = [HumanMessage(content="a")]
    h2 = [*h1, AIMessage(content="b"), HumanMessage(content="c")]
    cache.register(h1, "sid-1", thread_id="prof:t1")
    cache.register(h2, "sid-1")  # sibling fingerprint, same purged session
    other = [HumanMessage(content="z")]
    cache.register(other, "sid-2", thread_id="prof:t2")

    assert cache.invalidate("sid-1") == 3  # 2 fp: + 1 thread:

    assert cache.resolve([*h2, HumanMessage(content="d")]).strategy == "new"
    r = cache.resolve([*other, HumanMessage(content="q")])
    assert r.strategy == "resume" and r.session_id == "sid-2"  # ajena intacta
    order = (cache._store.get("__order__") or {}).get("keys", [])
    assert all(cache._store.get(k) is not None for k in order)  # LRU saneada


def test_invalidate_missing_session_is_noop():
    cache = SessionCache()
    cache.register([HumanMessage(content="a")], "sid-1")
    assert cache.invalidate("nope") == 0
    assert cache.invalidate("") == 0
    assert (
        cache.resolve([HumanMessage(content="a"), HumanMessage(content="b")]).session_id
        == "sid-1"
    )


# ── invoke path ──────────────────────────────────────────────


def test_stale_resume_degrades_to_new_session(monkeypatch):
    calls: list = []
    llm = _poisoned_llm(monkeypatch, calls)
    invoked = [*HIST, HumanMessage(content="¿sigues ahí?")]

    resp = llm.invoke(invoked)

    assert "sigo aquí" in _text(resp.content)
    # Exactly one doomed resume + one new run: the retry budget (max_retries=3)
    # was never spent re-running the doomed resume.
    assert calls == [STALE_ID, None]
    # Poisoned mappings gone (fp: AND thread:), new session registered.
    sids = _store_session_ids(llm)
    assert STALE_ID not in sids
    assert NEW_ID in sids
    # The next turn resumes the NEW session.
    nxt = llm._session_cache.resolve([*invoked, resp, HumanMessage(content="Di OK.")])
    assert nxt.strategy == "resume" and nxt.session_id == NEW_ID


def test_degraded_run_keeps_full_retry_budget(monkeypatch):
    calls: list = []
    llm = _poisoned_llm(monkeypatch, calls, max_retries=1, fail_new_runs=1)

    resp = llm.invoke([*HIST, HumanMessage(content="¿sigues ahí?")])

    # doomed resume (no budget) + new run failing once + new run retried OK:
    # with max_retries=1 this only works if the doomed resume consumed nothing.
    assert calls == [STALE_ID, None, None]
    assert "sigo aquí" in _text(resp.content)


def test_explicit_session_id_propagates(monkeypatch):
    import claude_agent_sdk

    calls: list = []
    monkeypatch.setattr(claude_agent_sdk, "query", _stale_then_ok_factory(calls))
    llm = ChatClaudeCli(model="haiku", max_retries=3, session_id=STALE_ID)

    with pytest.raises(ProcessError):
        llm.invoke([HumanMessage(content="hola")])

    # Fails fast: the caller pinned this exact session, so no silent new
    # session AND no doomed retries burning the budget.
    assert calls == [STALE_ID]


def test_explicit_session_id_via_config_kwarg_propagates(monkeypatch):
    import claude_agent_sdk

    calls: list = []
    monkeypatch.setattr(claude_agent_sdk, "query", _stale_then_ok_factory(calls))
    llm = ChatClaudeCli(model="haiku", max_retries=3)

    with pytest.raises(ProcessError):
        llm._generate(
            [HumanMessage(content="hola")],
            config={"configurable": {"session_id": STALE_ID}},
        )

    assert calls == [STALE_ID]


# ── streaming path ───────────────────────────────────────────


def test_stream_degrades_to_new_session(monkeypatch):
    calls: list = []
    llm = _poisoned_llm(monkeypatch, calls)
    invoked = [*HIST, HumanMessage(content="¿sigues ahí?")]

    chunks = list(llm.stream(invoked))

    text = "".join(c.content for c in chunks if isinstance(c.content, str))
    assert "sigo aquí" in text
    assert calls == [STALE_ID, None]
    sids = _store_session_ids(llm)
    assert STALE_ID not in sids
    assert NEW_ID in sids


def test_stream_explicit_session_id_propagates(monkeypatch):
    import claude_agent_sdk

    calls: list = []
    monkeypatch.setattr(claude_agent_sdk, "query", _stale_then_ok_factory(calls))
    llm = ChatClaudeCli(model="haiku", session_id=STALE_ID)

    with pytest.raises(ProcessError):
        list(llm.stream([HumanMessage(content="hola")]))

    assert calls == [STALE_ID]
