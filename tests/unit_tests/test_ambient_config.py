"""Unit tests for ambient RunnableConfig resolution and thread-key namespacing.

Regression guard for the defect these tests were written to catch: the session
recovery path read `kwargs["config"]`, which `BaseChatModel.invoke/ainvoke`
never populates. `SessionCache` itself was correct and its unit tests passed —
what was missing was a test that exercised the WIRING, i.e. a model invoked the
way LangGraph invokes it.
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from langchain_claude_cli import ChatClaudeCli
from langchain_claude_cli._runner import _effective_config
from langchain_claude_cli._sessions import FileStore

CFG = {"configurable": {"thread_id": "TID-42"}}


# ── _effective_config ────────────────────────────────────────


def test_effective_config_prefers_explicit_kwarg():
    explicit = {"configurable": {"thread_id": "explicit"}}
    assert _effective_config(explicit) is explicit


def test_effective_config_outside_runnable_is_empty():
    """No ambient config and no kwarg: same behavior as before the fix."""
    assert (_effective_config(None).get("configurable") or {}).get("thread_id") is None


# ── thread key reachability ──────────────────────────────────


def _model(**kw: Any) -> ChatClaudeCli:
    return ChatClaudeCli(model=kw.pop("model", "haiku"), **kw)


def test_thread_key_none_outside_runnable():
    assert _model()._thread_key(None) is None


def test_thread_key_from_explicit_config():
    key = _model()._thread_key(CFG)
    assert key is not None
    assert key.endswith(":TID-42")


def test_thread_key_from_ambient_config_inside_graph():
    """The wiring test that was missing: no config kwarg anywhere."""
    llm = _model()
    seen: list[str | None] = []

    def node(state: dict) -> dict:
        seen.append(llm._thread_key(None))  # exactly what _resolve_session does
        return {}

    g = StateGraph(dict)
    g.add_node("n", node)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    g.compile().invoke({}, config=CFG)

    assert seen[0] is not None, "ambient thread_id was not reachable"
    assert seen[0].endswith(":TID-42")


# ── profile namespacing ──────────────────────────────────────


def test_profile_differs_by_model_and_cwd(tmp_path):
    assert _model(model="haiku")._session_profile() != (
        _model(model="sonnet")._session_profile()
    )
    assert _model(cwd=str(tmp_path))._session_profile() != (_model()._session_profile())


def test_profile_ignores_system_prompt():
    """Runtimes recompose the system prompt every turn; it must not key sessions."""
    a = _model(system_prompt="you are A")._session_profile()
    b = _model(system_prompt="you are B, and today is Tuesday")._session_profile()
    assert a == b


def test_distinct_profiles_do_not_cross_sessions_on_one_thread(tmp_path):
    """A cheap router and an expensive executor share a graph thread."""
    store = FileStore(tmp_path / "s.json")
    router = _model(model="haiku", session_store=store)
    executor = _model(model="sonnet", session_store=store)

    executor._session_cache.register(
        [HumanMessage(content="q"), AIMessage(content="r")],
        "sess-executor",
        thread_id=executor._thread_key(CFG),
    )

    # Router: different history (no prefix match), same thread_id.
    res = router._resolve_session([HumanMessage(content="classify this")], CFG)
    assert res.strategy == "new", "router resumed the executor's session"

    # Same profile as the registrant still recovers.
    res2 = executor._resolve_session(
        [HumanMessage(content="q"), AIMessage(content="r"), HumanMessage(content="q2")],
        CFG,
    )
    assert res2.strategy == "resume"
    assert res2.session_id == "sess-executor"


def test_same_profile_recovers_across_system_prompt_changes(tmp_path):
    """Continuity must survive the per-turn system prompt recomposition."""
    store = FileStore(tmp_path / "s.json")
    turn1 = _model(system_prompt="prompt of turn 1", session_store=store)
    turn1._session_cache.register(
        [HumanMessage(content="q"), AIMessage(content="r")],
        "sess-1",
        thread_id=turn1._thread_key(CFG),
    )

    turn2 = _model(
        system_prompt="prompt of turn 2, with fresh memory",
        session_store=store,
    )
    res = turn2._resolve_session(
        [
            HumanMessage(content="q"),
            AIMessage(content="CLEANED"),
            HumanMessage(content="q2"),
        ],
        CFG,
    )
    assert res.strategy == "resume"
    assert res.session_id == "sess-1"
    assert [m.content for m in res.suffix] == ["q2"]


# ── explicit session_id still wins ───────────────────────────


def test_explicit_session_id_beats_thread_recovery():
    llm = _model(session_id="forced")
    res = llm._resolve_session(
        [HumanMessage(content="a"), HumanMessage(content="b")], CFG
    )
    assert res.strategy == "resume"
    assert res.session_id == "forced"
    assert [m.content for m in res.suffix] == ["b"]


@pytest.mark.parametrize("key", ["session_id"])
def test_configurable_session_id_from_ambient_config(key):
    llm = _model()
    cfg = {"configurable": {key: "from-config"}}
    res = llm._resolve_session([HumanMessage(content="a")], cfg)
    assert res.session_id == "from-config"
