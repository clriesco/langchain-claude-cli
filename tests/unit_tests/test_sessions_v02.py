"""Unit tests for v0.2 session persistence: FileStore, thread_id, replay mode."""

import json
import threading

from langchain_core.messages import AIMessage, HumanMessage

from langchain_claude_cli._sessions import (
    FileStore,
    InMemoryStore,
    SessionCache,
    make_store,
)


def _conv(n: int = 1):
    msgs = []
    for i in range(n):
        msgs += [HumanMessage(content=f"q{i}"), AIMessage(content=f"r{i}")]
    return msgs


def test_make_store_variants(tmp_path):
    assert isinstance(make_store(None), InMemoryStore)
    assert isinstance(make_store("memory"), InMemoryStore)
    assert isinstance(make_store("file"), FileStore)
    custom = InMemoryStore()
    assert make_store(custom) is custom


def test_filestore_roundtrip_and_atomicity(tmp_path):
    path = tmp_path / "sessions.json"
    store = FileStore(path)
    store.set("a", {"session_id": "s1"})
    assert store.get("a") == {"session_id": "s1"}
    # File is valid JSON on disk at all times
    assert json.loads(path.read_text())["a"]["session_id"] == "s1"
    store.delete("a")
    assert store.get("a") is None


def test_filestore_survives_new_instance(tmp_path):
    """Same file, fresh instance == process restart."""
    path = tmp_path / "sessions.json"
    cache1 = SessionCache(store=FileStore(path))
    cache1.register(_conv(), "sess-persisted")

    cache2 = SessionCache(store=FileStore(path))  # "new process"
    res = cache2.resolve([*_conv(), HumanMessage(content="new")])
    assert res.strategy == "resume"
    assert res.session_id == "sess-persisted"
    assert len(res.suffix) == 1


def test_filestore_concurrent_writers(tmp_path):
    path = tmp_path / "sessions.json"

    def writer(i: int):
        store = FileStore(path)
        for j in range(10):
            store.set(f"k{i}-{j}", {"session_id": f"s{i}-{j}"})

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # No corruption: file parses and has entries
    data = json.loads(path.read_text())
    assert len(data) >= 10


def test_thread_id_recovery_when_prefix_missing():
    cache = SessionCache()
    history = _conv(2)  # 4 messages
    cache.register(history, "sess-T", thread_id="th-1")

    # Checkpointer trimmed the history: prefix no longer matches,
    # but the thread has grown beyond the registered count.
    trimmed = [*history[2:], HumanMessage(content="q-new")]  # 3 msgs < count 4
    res = cache.resolve(trimmed, thread_id="th-1")
    assert res.strategy == "new"  # can't determine suffix -> safe fallback

    grown = [*history[:3], AIMessage(content="edited"), HumanMessage(content="q-new")]
    res2 = cache.resolve(grown, thread_id="th-1")
    assert res2.strategy == "resume"
    assert res2.session_id == "sess-T"
    assert [m.content for m in res2.suffix] == ["q-new"]


def test_lru_eviction_persists(tmp_path):
    cache = SessionCache(maxsize=2, store=FileStore(tmp_path / "s.json"))
    convs = [[HumanMessage(content=f"m{i}"), AIMessage(content="r")] for i in range(3)]
    for i, c in enumerate(convs):
        cache.register(c, f"s{i}")
    assert cache.resolve([*convs[0], HumanMessage(content="x")]).strategy == "new"
    assert cache.resolve([*convs[2], HumanMessage(content="x")]).session_id == "s2"
