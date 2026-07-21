# langchain-claude-cli

**Drop-in replacement for `ChatAnthropic`** that runs on the Claude Code CLI — use your Claude Pro/Max subscription, **no API key needed**.

Built on the official [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/) (≥ 0.2.115). Real tool calling via in-process MCP, native structured output, native extended thinking, real token usage — no prompt-injection hacks.

```bash
pip install langchain-claude-cli
```

## Quick Start

```python
from langchain_claude_cli import ChatClaudeCli

# Just like ChatAnthropic, but no API key
llm = ChatClaudeCli(model="claude-sonnet-4-5")
response = llm.invoke("What is the capital of France?")
print(response.content)
print(response.usage_metadata)   # real token usage, including cache tokens
```

### Prerequisites

- **Claude Code CLI** installed and authenticated: `npm install -g @anthropic-ai/claude-code`, then `claude` → log in
- **Claude Pro or Max subscription**
- Python ≥ 3.10, Node.js ≥ 18

## Feature parity with ChatAnthropic

Every `ChatAnthropic` constructor parameter is accepted — nothing breaks on migration. Parity comes in three levels:

### 🟢 Level A — Native

| Feature | Notes |
|---|---|
| `invoke` / `ainvoke` / `stream` / `astream` / `batch` | Real token-by-token streaming |
| Tool calling (`bind_tools`) | **Classic LangChain pattern**: model returns `AIMessage.tool_calls` without executing. Parallel tool calls supported |
| `with_structured_output` | CLI-native JSON-schema enforcement (`output_format`) |
| Extended thinking | Same config dict as ChatAnthropic: `thinking={"type": "enabled", "budget_tokens": N}` — plus `{"type": "adaptive"}` |
| `effort` | All five levels (`max/xhigh/high/medium/low`), passthrough |
| Token usage | `usage_metadata` incl. `cache_read`/`cache_creation` details, plus `total_cost_usd` in `response_metadata` |
| `stop_reason` | In `response_metadata`, like ChatAnthropic |
| Images (base64 + URL) | |
| PDFs (`document` blocks) | |
| System messages | |
| MCP servers | Both ChatAnthropic API-connector format and CLI-native (stdio/SSE/HTTP) |
| Server tools `web_search` / `web_fetch` | Mapped to the CLI's built-in WebSearch/WebFetch |
| `max_retries` / `timeout` | Client-side retry on 429/5xx; plus `fallback_model` |
| LangGraph agents | `create_agent` / `create_react_agent` work end-to-end |

### 🟡 Level B — Client-side workaround

| Feature | How |
|---|---|
| `stop_sequences` | Output scanned client-side; stream is cut and truncated at the sequence |
| `max_tokens` | Client-side truncation (~4 chars/token) with synthetic `stop_reason="max_tokens"` |
| `tool_choice="any"` / specific tool | System-prompt instruction + validation + one retry; explicit error if not satisfied |
| `get_num_tokens_from_messages` | Heuristic estimate (no count-tokens endpoint without an API key) |
| Arbitrary message histories | See [How conversations work](#how-conversations-work) below |

### 🔴 Level C — Accepted no-op (warns once)

`temperature`, `top_k`, `top_p`, `anthropic_api_url`, `anthropic_proxy`, `default_headers`, `inference_geo`, `context_management`, `cache_control` blocks (the CLI caches automatically — you still get cache token counts), citations, computer use, `strict` tool use.

## Tool calling — the classic LangChain pattern

Tools are registered as an in-process MCP server; a `PreToolUse` hook defers execution back to you. The model **never executes your tools** — it returns `tool_calls`, your code (or your LangGraph) executes them:

```python
from langchain_core.tools import tool

@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"25°C, sunny in {city}"

llm = ChatClaudeCli(model="claude-sonnet-4-5")
llm_with_tools = llm.bind_tools([get_weather])

response = llm_with_tools.invoke("What's the weather in Tokyo?")
response.tool_calls
# [{'name': 'get_weather', 'args': {'city': 'Tokyo'}, 'id': 'toolu_...'}]
```

Works out of the box with LangGraph:

```python
from langgraph.prebuilt import create_react_agent

agent = create_react_agent(model=llm, tools=[get_weather])
agent.invoke({"messages": [{"role": "user", "content": "Weather in Colombo?"}]})
```

## Structured output

```python
from pydantic import BaseModel

class Answer(BaseModel):
    answer: str
    confidence: float

structured = llm.with_structured_output(Answer)
structured.invoke("What is the capital of France?")
# Answer(answer='Paris', confidence=0.99)
```

Uses the CLI's native `output_format` (JSON-schema enforced by the model runtime, not by prompt begging). `include_raw=True` and dict/TypedDict schemas are supported.

## What's new in 0.2

- **Persistent sessions** — `session_store="file"`: conversations survive process restarts (the prefix-cache lives in `~/.langchain-claude-cli/`); LangGraph `thread_id` is used as a recovery path when checkpointers trim or normalize history. Inside a graph node the `thread_id` is read from the ambient config — nothing to wire (0.4.2). Recovery keys are namespaced by execution profile, so a cheap router and an expensive executor sharing one thread never resume each other's session. Turns that must *not* resume (heartbeats, crons, one-shot jobs) opt out by keeping the default `session_store="memory"` and building a fresh model per turn.
- **Persistent client** — `persistent=True`: a live CLI client per conversation (~2× faster reused turns), plus `set_session_model()` for hot model swaps.
- **`interrupt()`** (0.4): cancel active runs in ANY mode — persistent conversations via the CLI protocol, stateless invokes via task cancellation (the cancelled invoke raises `ClaudeCliInterruptedError`; the subprocess is cleaned up).
- **Typed errors** — `ClaudeCliRateLimitError`, `ClaudeCliOverloadedError`, `ClaudeCliAuthError`, `ClaudeCliTimeoutError`, `ClaudeCliBudgetExceededError`: build retry/fallback policies without parsing error text. Tip: set `max_retries=0` if your own fallback layer should see raw errors.
- **OAuth guard** — `auth="oauth"` (default) neutralizes an inherited `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` so the CLI can never silently bill your API account instead of using your subscription. `auth="inherit"` opts out.
- **Rate-limit visibility** — `response_metadata["rate_limit"]` reports your subscription window: `{status, type, utilization, resets_at}`.
- **`history_mode="replay"`** — replay arbitrary histories with full role fidelity (costs one generation per historical user message).
- **Files API blocks** — `file_id` sources are materialized via the Anthropic API when a key is available (used only for the download, never passed to the CLI) or dropped with a warning.
- **`ClaudeCodeToolsMiddleware`** — give ANY LangChain agent (any provider as orchestrator) a `claude_code` tool that delegates filesystem/shell work to a sandboxed, budget-capped Claude Code run:

```python
from langchain.agents import create_agent
from langchain_claude_cli.middleware import ClaudeCodeToolsMiddleware

agent = create_agent(
    model=any_chat_model,   # ChatOpenAI, ChatAnthropic, ChatClaudeCli...
    tools=[...],
    middleware=[ClaudeCodeToolsMiddleware(cwd="/workspace", max_budget_usd=0.5)],
)
```

> Production tip: always set `timeout` — if the CLI process is killed mid-run (e.g. subscription rate-limit exhaustion) the SDK stream can hang instead of raising.

## Reliability

- **Inactivity watchdog** (0.3): if the SDK stream goes silent (`inactivity_timeout`, default 120s in pure-LLM mode, disabled in agentic mode where slow tools are legitimate), the run aborts with `ClaudeCliTimeoutError` and the subprocess is cleaned up — a dead CLI can otherwise leave the stream open forever ([upstream issue](https://github.com/anthropics/claude-agent-sdk-python/issues/1110)).
- **Logging**: enable `logging.getLogger("langchain_claude_cli")` at DEBUG to see session resolution, pool activity, tool defer/delivery and retries.
- **Deterministic tests**: the core E2E suite runs against recorded cassettes (no CLI, no quota — `tests/cassette_tests`, refresh with `RECORD_CASSETTES=1`); a nightly [contract suite](.github/workflows/contract.yml) checks the live CLI still honors the behavior invariants the design depends on.
- `history_mode="replay"` is **experimental**: fidelity of injected assistant turns is race-dependent (contract finding).

## How conversations work

`BaseChatModel` is stateless; the CLI is a stateful session. The bridge is a **session prefix-cache**:

- A conversation that grows by appending (chatbots, agent loops, tool cycles) **resumes its CLI session** and sends only the new messages — full fidelity, and the CLI's automatic prompt caching keeps input tokens cheap.
- An arbitrary history with no known prefix (e.g. trimmed or hand-built) is **flattened into a single user message** — role-labelled text, with image/document blocks preserved. A `ClaudeCliCompatWarning` tells you when this happens.
- You can pin a CLI session explicitly: `llm.invoke(..., config={"configurable": {"session_id": "<uuid>"}})`.

## Agentic mode (opt-in)

By default the model runs with **no built-in tools** — pure-LLM semantics, same risk profile as an API call. Opt in to Claude Code's agentic capabilities:

```python
from langchain_claude_cli import ChatClaudeCli, READ_ONLY_TOOLS

# Read-only code analyst
analyst = ChatClaudeCli(
    model="claude-sonnet-4-5",
    builtin_tools=READ_ONLY_TOOLS,          # Read, Glob, Grep
    max_turns=10,
    permission_mode="bypassPermissions",
    cwd="/path/to/project",
)
analyst.invoke("Find all TODO comments and summarize them")

# Full agent (filesystem + bash) — trusted prompts only!
agent = ChatClaudeCli(
    model="claude-sonnet-4-5",
    builtin_tools="claude_code",            # everything
    permission_mode="bypassPermissions",
    max_budget_usd=1.0,                     # hard cost cap
    cwd="/path/to/project",
)
```

`builtin_tools` accepts a list of tool names / `ClaudeTool` enum values, or the `"claude_code"` preset. `allowed_tools`, `disallowed_tools`, `add_dirs`, `sandbox` and `max_budget_usd` map straight to the CLI. LangChain tools (deferred) and built-in tools (executed in-run) can be combined.

Agentic runs stream too: each built-in tool call the CLI executes is emitted as a `tool_use` content block in the stream, so you can render live activity ("→ Read data.txt") alongside the text tokens.

### Security

With `builtin_tools` + `bypassPermissions` the CLI subprocess runs as **your OS user**: prompt injection becomes code execution, and `cwd` does **not** sandbox file access. Never enable agentic mode on untrusted input; prefer `READ_ONLY_TOOLS`, `disallowed_tools=["Bash"]`, `sandbox`, and containers for production. Pure-LLM mode (the default) has none of these risks.

## Migration

### From ChatAnthropic

```python
# Before
from langchain_anthropic import ChatAnthropic
llm = ChatAnthropic(model="claude-sonnet-4-5", api_key="sk-ant-...")

# After — everything else stays the same
from langchain_claude_cli import ChatClaudeCli
llm = ChatClaudeCli(model="claude-sonnet-4-5")
```

### From langchain-claude-code (the old library)

| Old (`ChatClaudeCode`) | New (`ChatClaudeCli`) |
|---|---|
| `ChatClaudeCode(...)` | `ChatClaudeCli(...)` |
| `bind_tools` via prompt injection | Real MCP-based tool calling |
| `thinking` (prompt text hack) | Native extended thinking |
| Token usage unavailable | Full `usage_metadata` |
| `max_turns=5` to enable tools | `builtin_tools=[...]` (explicit opt-in) |
| History flattened to text | Session resume with full fidelity |

## ⚖️ Legal & Terms of Service

> **Disclaimer:** community project, **not affiliated with or endorsed by Anthropic**. You are responsible for complying with Anthropic's terms.

This package uses the official, MIT-licensed `claude-agent-sdk` published by Anthropic — no reverse engineering, no credential extraction. Your usage is governed by Anthropic's [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) (Pro/Max) or [Commercial Terms](https://www.anthropic.com/legal/commercial-terms) (API), and the [Acceptable Use Policy](https://www.anthropic.com/legal/aup). Notably: consumer subscriptions are for individual use, may not be resold or used to power products for end users, and heavy automated usage counts against your subscription's rate limits. **For anything beyond personal/internal use, use an Anthropic API key under the Commercial Terms** (and then you likely want `langchain-anthropic` directly).

## License

MIT
