"""Unit tests for the session prefix-cache."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from langchain_claude_cli._sessions import SessionCache, prefix_fingerprints


def test_fingerprint_stable_and_order_sensitive():
    msgs = [HumanMessage(content="a"), AIMessage(content="b")]
    assert prefix_fingerprints(msgs) == prefix_fingerprints(list(msgs))
    swapped = [AIMessage(content="b"), HumanMessage(content="a")]
    assert prefix_fingerprints(msgs)[-1] != prefix_fingerprints(swapped)[-1]


def test_fingerprint_ignores_volatile_metadata():
    a1 = AIMessage(content="hi", response_metadata={"session_id": "x"}, id="run-1")
    a2 = AIMessage(content="hi", response_metadata={"session_id": "y"}, id="run-2")
    h = HumanMessage(content="q")
    assert prefix_fingerprints([h, a1])[-1] == prefix_fingerprints([h, a2])[-1]


def test_resolve_growing_conversation():
    cache = SessionCache()
    h1, a1 = HumanMessage(content="q1"), AIMessage(content="r1")
    cache.register([h1, a1], "sess-1")

    res = cache.resolve([h1, a1, HumanMessage(content="q2")])
    assert res.strategy == "resume"
    assert res.session_id == "sess-1"
    assert len(res.suffix) == 1
    assert res.suffix[0].content == "q2"


def test_resolve_unknown_history_is_new():
    cache = SessionCache()
    res = cache.resolve([HumanMessage(content="never seen")])
    assert res.strategy == "new"
    assert len(res.suffix) == 1


def test_resolve_longest_prefix_wins():
    cache = SessionCache()
    h1, a1 = HumanMessage(content="q1"), AIMessage(content="r1")
    h2, a2 = HumanMessage(content="q2"), AIMessage(content="r2")
    cache.register([h1, a1], "short")
    cache.register([h1, a1, h2, a2], "long")
    res = cache.resolve([h1, a1, h2, a2, HumanMessage(content="q3")])
    assert res.session_id == "long"
    assert len(res.suffix) == 1


def test_interleaved_conversations_do_not_cross():
    cache = SessionCache()
    a_h, a_a = HumanMessage(content="conv A"), AIMessage(content="ra")
    b_h, b_a = HumanMessage(content="conv B"), AIMessage(content="rb")
    cache.register([a_h, a_a], "sess-A")
    cache.register([b_h, b_a], "sess-B")
    assert cache.resolve([a_h, a_a, HumanMessage(content="x")]).session_id == "sess-A"
    assert cache.resolve([b_h, b_a, HumanMessage(content="x")]).session_id == "sess-B"


def test_tool_cycle_prefix_match():
    cache = SessionCache()
    h = HumanMessage(content="weather?")
    ai = AIMessage(
        content="", tool_calls=[{"name": "w", "args": {"c": "T"}, "id": "t1"}]
    )
    cache.register([h, ai], "sess-T")
    res = cache.resolve([h, ai, ToolMessage(content="25C", tool_call_id="t1")])
    assert res.strategy == "resume"
    assert res.session_id == "sess-T"
    assert isinstance(res.suffix[0], ToolMessage)


def test_lru_eviction():
    cache = SessionCache(maxsize=2)
    msgs = [[HumanMessage(content=f"m{i}")] for i in range(3)]
    for i, m in enumerate(msgs):
        cache.register(m, f"s{i}")
    assert cache.resolve([*msgs[0], HumanMessage(content="x")]).strategy == "new"
    assert cache.resolve([*msgs[2], HumanMessage(content="x")]).session_id == "s2"
