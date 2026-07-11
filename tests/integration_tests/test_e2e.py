"""Integration tests — require an authenticated `claude` CLI (Pro/Max).

Run explicitly:  pytest tests/integration_tests -m integration
Each test uses claude-haiku-4-5 and short prompts to limit quota use.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from langchain_claude_cli import ChatClaudeCli

pytestmark = pytest.mark.integration

MODEL = "claude-haiku-4-5"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    )


@pytest.fixture()
def llm() -> ChatClaudeCli:
    return ChatClaudeCli(model=MODEL, timeout=120)


@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"25C sunny in {city}"


@tool
def calculate(expression: str) -> str:
    """Evaluate a math expression."""
    return str(eval(expression))


# ── chat-model-core ──────────────────────────────────────────


def test_invoke_basic(llm):
    result = llm.invoke("Reply with exactly: PONG")
    assert "PONG" in _text(result.content)
    assert result.usage_metadata["output_tokens"] > 0
    assert result.response_metadata["session_id"]
    assert result.response_metadata["stop_reason"] == "end_turn"


async def test_ainvoke(llm):
    result = await llm.ainvoke("Reply with exactly: PONG")
    assert "PONG" in _text(result.content)


def test_multi_turn_resumes_session(llm):
    h1 = HumanMessage(content="My favorite number is 77. Say OK.")
    a1 = llm.invoke([h1])
    r = llm.invoke(
        [h1, a1, HumanMessage(content="What is my favorite number? Digits only.")]
    )
    assert "77" in _text(r.content)
    assert r.response_metadata["session_id"] == a1.response_metadata["session_id"]


# ── tool-calling ─────────────────────────────────────────────


def test_tool_call_emitted_without_execution(llm):
    executed = []

    @tool
    def spy_tool(x: str) -> str:
        """Record execution."""
        executed.append(x)
        return x

    r = llm.bind_tools([spy_tool]).invoke("Call spy_tool with x='hi'.")
    assert r.tool_calls and r.tool_calls[0]["name"] == "spy_tool"
    assert executed == []


def test_full_tool_cycle(llm):
    lt = llm.bind_tools([get_weather])
    msgs = [HumanMessage(content="Weather in Tokyo? Use get_weather.")]
    r1 = lt.invoke(msgs)
    assert r1.tool_calls
    tc = r1.tool_calls[0]
    msgs += [
        r1,
        ToolMessage(content=get_weather.invoke(tc["args"]), tool_call_id=tc["id"]),
    ]
    r2 = lt.invoke(msgs)
    assert not r2.tool_calls
    assert "25" in _text(r2.content)


def test_agent_loop(llm):
    try:
        from langchain.agents import create_agent
    except ImportError:  # langchain 1.x not installed; legacy fallback
        from langgraph.prebuilt import create_react_agent as create_agent

    agent = create_agent(model=llm, tools=[get_weather, calculate])
    out = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Weather in Colombo? Then compute 17*23 with calculate.",
                }
            ]
        }
    )
    final = _text(out["messages"][-1].content)
    assert "25" in final and "391" in final


# ── structured-output ────────────────────────────────────────


class Answer(BaseModel):
    answer: str
    confidence: float


def test_structured_output_pydantic(llm):
    result = llm.with_structured_output(Answer).invoke("Capital of France?")
    assert isinstance(result, Answer)
    assert "aris" in result.answer


def test_structured_output_include_raw(llm):
    out = llm.with_structured_output(Answer, include_raw=True).invoke(
        "Capital of France?"
    )
    assert out["parsing_error"] is None
    assert isinstance(out["parsed"], Answer)
    assert out["raw"].response_metadata["session_id"]


# ── streaming ────────────────────────────────────────────────


def test_stream_multiple_chunks_and_usage(llm):
    chunks = list(llm.stream("Count from 1 to 5, digits separated by spaces."))
    text = "".join(c.content for c in chunks if isinstance(c.content, str))
    assert len(chunks) > 2
    assert "5" in text
    assert any(c.usage_metadata for c in chunks)


async def test_astream(llm):
    text = ""
    async for chunk in llm.astream("Say: hello world"):
        if isinstance(chunk.content, str):
            text += chunk.content
    assert "hello" in text.lower()


def test_stream_stop_sequence(llm):
    chunks = list(llm.stream("Repeat exactly: alpha ### beta", stop=["###"]))
    text = "".join(c.content for c in chunks if isinstance(c.content, str))
    assert "###" not in text
    assert "beta" not in text


# ── multimodal ───────────────────────────────────────────────

_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 60>>stream\nBT /F1 24 Tf 100 700 Td "
    b"(Secret word: XYLOPHONE-99) Tj ET\nendstream\nendobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF"
)


def test_pdf_document_block(llm):
    import base64

    r = llm.invoke(
        [
            HumanMessage(
                content=[
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.b64encode(_PDF).decode(),
                        },
                    },
                    {"type": "text", "text": "Secret word in the PDF? Word only."},
                ]
            )
        ]
    )
    assert "XYLOPHONE" in _text(r.content).upper()


# ── agentic-mode ─────────────────────────────────────────────


def test_agentic_read_only(tmp_path):
    (tmp_path / "data.txt").write_text("MAGIC=ZANZIBAR42")
    agent = ChatClaudeCli(
        model=MODEL,
        builtin_tools=["Read", "Glob", "Grep"],
        max_turns=5,
        permission_mode="bypassPermissions",
        cwd=str(tmp_path),
    )
    r = agent.invoke("Read data.txt and tell me the value of MAGIC. Value only.")
    assert "ZANZIBAR42" in _text(r.content)


def test_pure_llm_mode_has_no_tools(llm, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    r = llm.invoke(f"Read the file {secret} and print its contents.")
    assert "TOPSECRET" not in _text(r.content)


def test_agentic_stream_shows_tool_activity(tmp_path):
    (tmp_path / "data.txt").write_text("MAGIC=ZANZIBAR42")
    agent = ChatClaudeCli(
        model=MODEL,
        builtin_tools=["Read"],
        max_turns=5,
        permission_mode="bypassPermissions",
        cwd=str(tmp_path),
    )
    tool_blocks, text = [], ""
    for chunk in agent.stream("Read data.txt and tell me the value of MAGIC."):
        if isinstance(chunk.content, str):
            text += chunk.content
        else:
            tool_blocks += [
                b
                for b in chunk.content
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
    assert any(b["name"] == "Read" for b in tool_blocks), tool_blocks
    assert "ZANZIBAR42" in text


def test_max_budget_exceeded_raises_without_retry():
    import time

    from langchain_claude_cli import ClaudeCliBudgetExceededError

    llm = ChatClaudeCli(model=MODEL, max_budget_usd=0.000001, max_retries=2)
    t0 = time.time()
    with pytest.raises(ClaudeCliBudgetExceededError, match="budget"):
        llm.invoke("Write a haiku about rivers.")
    # A retried budget error would take 3 full runs (+backoff); one run is fast.
    assert time.time() - t0 < 30


def test_sandbox_blocks_write_outside_workspace(tmp_path):
    import pathlib

    escape = pathlib.Path.home() / "sandbox_escape_test_DELETE_ME.txt"
    escape.unlink(missing_ok=True)
    agent = ChatClaudeCli(
        model=MODEL,
        builtin_tools=["Bash"],
        max_turns=6,
        permission_mode="bypassPermissions",
        cwd=str(tmp_path),
        sandbox={"enabled": True, "autoAllowBashIfSandboxed": True},
    )
    try:
        r = agent.invoke(
            f"Run exactly this bash command and report the outcome: echo hola > {escape}"
        )
        assert not escape.exists(), "sandbox escape: file was created outside cwd"
        assert _text(r.content)  # the model reports the denial instead of crashing
    finally:
        escape.unlink(missing_ok=True)


def test_sandbox_allows_write_inside_workspace(tmp_path):
    agent = ChatClaudeCli(
        model=MODEL,
        builtin_tools=["Bash"],
        max_turns=6,
        permission_mode="bypassPermissions",
        cwd=str(tmp_path),
        sandbox={"enabled": True, "autoAllowBashIfSandboxed": True},
    )
    agent.invoke("Run: echo hola > inside.txt — then confirm it exists with ls.")
    assert (tmp_path / "inside.txt").exists()


# ── v0.2: session persistence ────────────────────────────────


def test_conversation_survives_restart(tmp_path):
    """Two model instances sharing a FileStore == two processes."""
    from langchain_claude_cli._sessions import FileStore

    store_path = tmp_path / "sessions.json"
    llm1 = ChatClaudeCli(model=MODEL, session_store=FileStore(store_path))
    h1 = HumanMessage(content="My secret word is PLUTONIO. Say OK.")
    a1 = llm1.invoke([h1])

    llm2 = ChatClaudeCli(model=MODEL, session_store=FileStore(store_path))
    r = llm2.invoke(
        [h1, a1, HumanMessage(content="What is my secret word? Word only.")]
    )
    assert "PLUTONIO" in _text(r.content).upper()
    assert r.response_metadata["session_id"] == a1.response_metadata["session_id"]


def test_history_replay_mode(llm):
    from langchain_core.messages import AIMessage

    replay_llm = ChatClaudeCli(model=MODEL, history_mode="replay")
    r = replay_llm.invoke(
        [
            HumanMessage(content="Invent a codename."),
            AIMessage(content="Your codename is KRAKEN-7."),
            HumanMessage(content="What codename did you give me? Codename only."),
        ]
    )
    assert "KRAKEN" in _text(r.content).upper()


# ── v0.2: persistent client ──────────────────────────────────


def test_persistent_multiturn_reuses_client():
    import time as _time

    llm = ChatClaudeCli(model=MODEL, persistent=True)
    try:
        h1 = HumanMessage(content="My lucky number is 13. Say OK.")
        a1 = llm.invoke([h1])
        _time.sleep(1.5)  # let the warm-up connect finish
        t0 = _time.time()
        r = llm.invoke(
            [h1, a1, HumanMessage(content="Lucky number plus 1? Digits only.")]
        )
        reuse_latency = _time.time() - t0
        assert "14" in _text(r.content)
        assert len(llm._pool) >= 1, "pool never warmed"
        assert reuse_latency < 10
        assert r.response_metadata["session_id"] == a1.response_metadata["session_id"]
    finally:
        llm._pool.close()


def test_persistent_interrupt_then_next_invoke():
    import threading as _threading
    import time as _time

    llm = ChatClaudeCli(model=MODEL, persistent=True)
    try:
        h1 = HumanMessage(content="Say READY.")
        a1 = llm.invoke([h1])
        _time.sleep(1.5)
        done = []
        history = [
            h1,
            a1,
            HumanMessage(content="Count slowly from 1 to 500, one per line."),
        ]
        t = _threading.Thread(target=lambda: done.append(llm.invoke(history)))
        t.start()
        _time.sleep(4)
        llm.interrupt()
        t.join(timeout=30)
        assert not t.is_alive(), "interrupt did not end the generation"
        # The conversation still works after the interrupt
        r = llm.invoke([h1, a1, HumanMessage(content="Say DONE.")])
        assert "DONE" in _text(r.content).upper()
    finally:
        llm._pool.close()


# ── v0.2: middleware ─────────────────────────────────────────


def test_middleware_delegates_filesystem_task(tmp_path):
    from langchain.agents import create_agent

    from langchain_claude_cli.middleware import ClaudeCodeToolsMiddleware

    (tmp_path / "notes.txt").write_text("MAGIC=QUIMERA88")
    agent = create_agent(
        model=ChatClaudeCli(model=MODEL, timeout=120),
        tools=[],
        middleware=[
            ClaudeCodeToolsMiddleware(
                model=MODEL,
                builtin_tools=["Read", "Glob"],
                cwd=str(tmp_path),
                timeout=120,
            )
        ],
    )
    out = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Use claude_code to read notes.txt in the workspace "
                    "and tell me the value of MAGIC.",
                }
            ]
        }
    )
    assert "QUIMERA88" in _text(out["messages"][-1].content)


def test_middleware_budget_error_does_not_break_graph(tmp_path):
    from langchain.agents import create_agent

    from langchain_claude_cli.middleware import ClaudeCodeToolsMiddleware

    agent = create_agent(
        model=ChatClaudeCli(model=MODEL, timeout=120),
        tools=[],
        middleware=[
            ClaudeCodeToolsMiddleware(
                model=MODEL,
                builtin_tools=["Read"],
                cwd=str(tmp_path),
                max_budget_usd=0.000001,
                timeout=120,
            )
        ],
    )
    out = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Use claude_code to list the files in the workspace. "
                    "If it fails, report the failure reason.",
                }
            ]
        }
    )
    final = _text(out["messages"][-1].content).lower()
    assert final, "graph broke instead of completing"
    assert "budget" in final or "fail" in final or "error" in final or "unable" in final
