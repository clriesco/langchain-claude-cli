# langchain-claude-cli

**Drop-in replacement for `ChatAnthropic`** that runs on the Claude Code CLI — use your Claude Pro/Max subscription, **no API key needed**.

Built on Anthropic's official [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/): real tool calling through an in-process MCP server, native structured output, native extended thinking, real token accounting. No prompt-injection hacks.

```bash
pip install langchain-claude-cli
```

## Requirements

- **Claude Code CLI**, installed and authenticated — `npm install -g @anthropic-ai/claude-code`, then run `claude` and log in
- **Claude Pro or Max subscription**
- Python ≥ 3.10, Node.js ≥ 18

No API key, and no configuration: the library reuses the login the CLI already has. On a server, where nobody can log in interactively, see [Headless deployment](#headless-deployment).

## Quick start

```python
from langchain_claude_cli import ChatClaudeCli

llm = ChatClaudeCli(model="claude-sonnet-4-5")

response = llm.invoke("What is the capital of France?")
print(response.content)
print(response.usage_metadata)      # real token counts, cache reads included
```

`invoke`, `ainvoke`, `stream`, `astream` and `batch` all work, with genuine token-by-token streaming.

## What you get

- **Real tool calling** via `bind_tools` — the model returns `tool_calls`, your code executes them
- **Structured output** enforced by the CLI's native JSON-schema mode, not by asking nicely
- **Extended thinking** and the five `effort` levels, passed straight through
- **Session continuity** — multi-turn conversations resume the underlying CLI session instead of resending history
- **Agentic mode** (opt-in) — filesystem, shell and web tools, with budget caps and sandboxing
- **Typed errors** and configurable retries, so you can build real fallback policies
- **LangGraph compatible** — `create_agent` / `create_react_agent` work end to end

## Tool calling

Tools are registered as an in-process MCP server, and a `PreToolUse` hook defers execution back to you. The model **never runs your tools** — it returns `tool_calls` and your code (or your graph) decides what happens:

```python
from langchain_core.tools import tool

@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"25°C, sunny in {city}"

llm_with_tools = ChatClaudeCli(model="claude-sonnet-4-5").bind_tools([get_weather])

response = llm_with_tools.invoke("What's the weather in Tokyo?")
response.tool_calls
# [{'name': 'get_weather', 'args': {'city': 'Tokyo'}, 'id': 'toolu_...'}]
```

Parallel tool calls are supported. With LangGraph, nothing special is needed:

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

Pydantic models (v1 and v2), plain dicts and `TypedDict` schemas are accepted, as is `include_raw=True`. Streaming works too — the parsed object arrives as the last chunk.

## Conversations and sessions

`BaseChatModel` is stateless; the CLI is a stateful session. The bridge between them is a **session prefix-cache**:

- A history that grows by appending — chatbots, agent loops, tool cycles — **resumes its CLI session** and sends only the new messages. Full fidelity, and the CLI's automatic prompt caching keeps input tokens cheap.
- A history with no recognizable prefix (trimmed, reordered, hand-built) is **flattened into a single role-labelled user message**, preserving image and document blocks. A `ClaudeCliCompatWarning` tells you when that happens.
- `session_store="file"` persists the cache in `~/.langchain-claude-cli/`, so conversations survive process restarts. Inside a LangGraph node the `thread_id` is picked up automatically as a recovery path.
- `ChatClaudeCli(session_id="<uuid>")` pins an explicit CLI session.

For long-lived processes, `persistent=True` keeps a live client per conversation — roughly twice as fast on reused turns, and it enables `interrupt()` and `set_session_model()`.

`interrupt()` works in every mode, and the cancelled call raises `ClaudeCliInterruptedError`. Note that an interrupted turn ends its CLI session: the session is left holding an unfinished reply, and resuming it would make the model continue that reply instead of answering your next message. The following turn therefore re-sends its history into a fresh session — the conversation survives, its prompt cache does not.

### History modes

`history_mode` decides what happens when a history *cannot* be resolved to a known session:

- **`"auto"`** (default) — resume when the prefix is recognized, flatten otherwise.
- **`"flatten"`** — always flatten, even when a resume was possible.
- **`"replay"`** (experimental) — inject the history as separate `user`/`assistant` entries instead of flattening.

Replay looks like the obvious right answer and mostly isn't. The CLI's input is a *live conversation*, not a transcript upload: every historical `user` entry triggers a real generation, so cost grows with history length, and the fabricated assistant turn races against the model's own live reply — it may keep its own and discard yours. Fidelity is genuinely non-deterministic, which is why the invariant ships as a nightly `xfail` rather than a guarantee. Use it only when role fidelity matters more than cost and determinism.

### Session stores

The cache maps history fingerprints to CLI session ids. It stores **only SHA-256 digests and session ids — never message content** — under two key shapes, plus a reserved `__order__` entry that keeps the LRU (256 entries) durable:

```
fp:<sha256 of the history prefix>       → {"session_id": "..."}
thread:<profile digest>:<thread_id>     → {"session_id": "...", "count": N}
```

`"memory"` is per-instance and dies with the process. `"file"` writes `~/.langchain-claude-cli/sessions.json` with POSIX locking and atomic renames; the worst case under concurrent writers is a lost entry, which degrades that conversation to flatten — never corruption. For a custom path, pass the store directly:

```python
from langchain_claude_cli import ChatClaudeCli, FileStore

llm = ChatClaudeCli(session_store=FileStore("/var/lib/myapp/sessions.json"))
```

`FileStore` assumes a shared filesystem, so once you run more than one worker you'll want a real backend. Any object implementing `SessionStoreBackend` works:

```python
import json
from langchain_claude_cli import ChatClaudeCli, SessionStoreBackend

class RedisStore(SessionStoreBackend):
    def __init__(self, client, prefix="lcc:"):
        self._r, self._p = client, prefix

    def get(self, key):
        raw = self._r.get(self._p + key)
        return json.loads(raw) if raw else None

    def set(self, key, value):
        self._r.set(self._p + key, json.dumps(value))

    def keys(self):
        return [k.decode().removeprefix(self._p) for k in self._r.scan_iter(f"{self._p}*")]

    def delete(self, key):
        self._r.delete(self._p + key)

llm = ChatClaudeCli(session_store=RedisStore(redis_client))
```

`SessionCache` serializes its own calls, so a backend only needs internal locking when the same store is shared across processes.

One caveat worth knowing: the store maps to sessions, but the transcripts live inside the CLI, which prunes inactive sessions after `cleanupPeriodDays` (~30 days). A persistent store therefore outlives what it points at. That case is handled — a purged session is detected on the first attempt, every mapping pointing at it is invalidated, and the turn transparently re-runs as a new session with its retry budget intact.

## Agentic mode

By default the model runs with **no built-in tools**: pure-LLM semantics, the same risk profile as an API call. Claude Code's own capabilities are opt-in:

```python
from langchain_claude_cli import ChatClaudeCli, READ_ONLY_TOOLS

analyst = ChatClaudeCli(
    model="claude-sonnet-4-5",
    builtin_tools=READ_ONLY_TOOLS,        # Read, Glob, Grep
    max_turns=10,
    permission_mode="bypassPermissions",
    cwd="/path/to/project",
)
analyst.invoke("Find all TODO comments and summarize them")
```

`builtin_tools` takes a list of tool names or `ClaudeTool` values, or the `"claude_code"` preset for everything. Ready-made groups: `READ_ONLY_TOOLS`, `WRITE_TOOLS`, `SHELL_TOOLS`, `NETWORK_TOOLS`, `ALL_TOOLS`. LangChain tools (deferred to you) and built-in tools (executed in-run) can be combined, and each built-in call is emitted as a `tool_use` block in the stream so you can render live activity.

You can also hand Claude Code's toolset to an agent driven by *any* provider:

```python
from langchain.agents import create_agent
from langchain_claude_cli.middleware import ClaudeCodeToolsMiddleware

agent = create_agent(
    model=any_chat_model,               # ChatOpenAI, ChatAnthropic, ChatClaudeCli...
    tools=[...],
    middleware=[ClaudeCodeToolsMiddleware(cwd="/workspace", max_budget_usd=0.5)],
)
```

### Security

With `builtin_tools` plus `bypassPermissions`, the CLI subprocess runs as **your OS user**: prompt injection becomes code execution, and `cwd` does *not* sandbox file access. Never enable agentic mode on untrusted input. Prefer `READ_ONLY_TOOLS`, `disallowed_tools=["Bash"]`, `sandbox`, and containers in production. The default pure-LLM mode carries none of these risks.

## Error handling

Every failure mode is a typed exception, so retry and fallback logic never has to parse error text:

`ClaudeCliError` · `ClaudeCliAuthError` · `ClaudeCliRateLimitError` · `ClaudeCliOverloadedError` · `ClaudeCliTimeoutError` · `ClaudeCliBudgetExceededError` · `ClaudeCliInterruptedError`

Retries on 429/5xx are handled for you (`max_retries`, default 2) — set `max_retries=0` if your own fallback layer should see the raw error. `response_metadata["rate_limit"]` reports your subscription window as `{status, type, utilization, resets_at}`.

Two things worth setting in production: `timeout`, and `inactivity_timeout` (default `"auto"` — 120s in pure-LLM mode, off in agentic mode where slow tools are legitimate). If the CLI process dies mid-run, the SDK stream can otherwise hang open forever.

Debug logging: `logging.getLogger("langchain_claude_cli")` at `DEBUG` shows session resolution, pool activity, tool defer/delivery and retries.

## Options

### Model

| Option | Default | Description |
|---|---|---|
| `model` | `"claude-sonnet-4-5"` | Model id (alias: `model_name`) |
| `system_prompt` | `None` | Extra system prompt, prepended to any `SystemMessage` |
| `thinking` | `None` | `{"type": "enabled", "budget_tokens": N}`, or `{"type": "adaptive"}` |
| `effort` | `None` | `max` / `xhigh` / `high` / `medium` / `low` |
| `max_tokens` | `None` | Client-side truncation (see compatibility below) |
| `stop_sequences` | `None` | Client-side, output is cut at the sequence (alias: `stop`) |
| `betas` | `None` | Beta feature flags passed to the CLI |
| `mcp_servers` | `None` | ChatAnthropic connector format or CLI-native stdio/SSE/HTTP |

### Sessions and performance

| Option | Default | Description |
|---|---|---|
| `session_store` | `"memory"` | `"memory"`, `"file"` (persistent), or a `SessionStoreBackend` |
| `session_id` | `None` | Resume an explicit CLI session |
| `history_mode` | `"auto"` | `"auto"` resume-then-flatten, `"flatten"` always, `"replay"` (experimental) |
| `persistent` | `False` | Keep a live client per conversation |
| `pool_max_clients` | `4` | Max live clients when `persistent=True` |
| `pool_ttl` | `300.0` | Seconds an idle pooled client is kept |

### Agentic

| Option | Default | Description |
|---|---|---|
| `builtin_tools` | `None` | `None` = pure LLM; a tool list; or the `"claude_code"` preset |
| `allowed_tools` / `disallowed_tools` | `None` | Fine-grained allow/deny lists |
| `permission_mode` | `None` | `default` / `acceptEdits` / `plan` / `bypassPermissions` |
| `max_turns` | `None` | 1 in pure-LLM mode, unlimited in agentic mode |
| `cwd` / `add_dirs` | `None` | Working directory and extra readable roots |
| `sandbox` | `None` | CLI sandbox configuration |
| `max_budget_usd` | `None` | Hard cost cap per run |

### Reliability

| Option | Default | Description |
|---|---|---|
| `max_retries` | `2` | Client-side retries on 429/5xx |
| `timeout` | `None` | Total run timeout, in seconds |
| `inactivity_timeout` | `"auto"` | Abort if the stream goes silent; `None` disables |
| `fallback_model` | `None` | Model to fall back to when the primary fails |

### Environment and auth

| Option | Default | Description |
|---|---|---|
| `auth` | `"oauth"` | Neutralizes inherited `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` so the CLI always uses your subscription. `"inherit"` opts out |
| `env` | `None` | Extra environment variables for the CLI subprocess |
| `cli_path` | `None` | Path to a specific `claude` binary |

## Headless deployment

Interactive login is not an option in a container or on CI. Mint a long-lived token on a machine where you *are* logged in:

```bash
claude setup-token      # prints a 1-year, inference-scoped OAuth token
```

From there it only has to reach the process environment — the CLI subprocess inherits it.

### From a `.env` file

The usual choice in development. **Nothing in this package reads `.env`**, so load it yourself before the model runs:

```bash
# .env — never commit this
CLAUDE_CODE_OAUTH_TOKEN=<the token setup-token printed>
```

```python
from dotenv import load_dotenv          # pip install python-dotenv
from langchain_claude_cli import ChatClaudeCli

load_dotenv()                           # must run before the first invoke
llm = ChatClaudeCli(model="claude-sonnet-4-5")
```

Runtimes that read env files themselves need no `load_dotenv()` — `env_file:` in Docker Compose, `EnvironmentFile=` in a systemd unit, or your platform's secret store. Either way the variable is set for the whole process.

### Per instance

When one process serves several accounts, pass the token explicitly instead. `env` is merged on top of the inherited environment, so each instance can carry a different one:

```python
llm = ChatClaudeCli(
    model="claude-sonnet-4-5",
    env={"CLAUDE_CODE_OAUTH_TOKEN": token_for_this_user},
)
```

This also overrides any local `claude` login, which is what makes it work on a developer machine already authenticated as someone else.

### What to watch for

- **No refresh token.** These tokens last a year and cannot renew themselves. When one expires the CLI simply fails — mint a new one and restart the process.
- **Keep `auth="oauth"`.** `ANTHROPIC_AUTH_TOKEN` outranks `CLAUDE_CODE_OAUTH_TOKEN` in the CLI's auth precedence; the default guard neutralizes it so a stray value in the environment cannot silently redirect your billing.
- **Isolate tenants.** Several accounts in one container should each get their own `CLAUDE_CONFIG_DIR` in the same `env` dict, or they will share session state.

## ChatAnthropic compatibility

Every `ChatAnthropic` constructor parameter is accepted — nothing raises on migration, so it is a two-line change:

```python
# Before
from langchain_anthropic import ChatAnthropic
llm = ChatAnthropic(model="claude-sonnet-4-5", api_key="sk-ant-...")

# After
from langchain_claude_cli import ChatClaudeCli
llm = ChatClaudeCli(model="claude-sonnet-4-5")
```

Support comes in three levels:

**Native.** Streaming, tool calling, structured output, extended thinking, `effort`, token usage (including cache reads and `total_cost_usd`), `stop_reason`, images, PDFs, system messages, MCP servers, `web_search` / `web_fetch`, `max_retries`, `timeout`, `fallback_model`, LangGraph agents.

**Client-side workaround.** `stop_sequences` (output scanned and cut client-side), `max_tokens` (truncation at ~4 chars/token with a synthetic `stop_reason`), `tool_choice` (system-prompt instruction, validated, one retry, then an explicit error), `get_num_tokens_from_messages` (heuristic — there is no count-tokens endpoint without an API key), arbitrary histories (see [Conversations and sessions](#conversations-and-sessions)).

**Accepted no-op**, warned once: `temperature`, `top_k`, `top_p`, `anthropic_api_url`, `anthropic_proxy`, `default_headers`, `inference_geo`, `context_management`, `cache_control` blocks (the CLI caches automatically; you still get the token counts), citations, computer use, `strict` tool use.

## Legal

> Community project — **not affiliated with or endorsed by Anthropic.** You are responsible for complying with Anthropic's terms.

This package builds on the official, MIT-licensed `claude-agent-sdk`. No reverse engineering, no credential extraction. Your usage is governed by Anthropic's [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) (Pro/Max) or [Commercial Terms](https://www.anthropic.com/legal/commercial-terms) (API), and the [Acceptable Use Policy](https://www.anthropic.com/legal/aup).

In particular: consumer subscriptions are for individual use, may not be resold or used to power a product for end users, and heavy automated usage counts against your own rate limits. **For anything beyond personal or internal use, use an Anthropic API key under the Commercial Terms** — at which point you probably want `langchain-anthropic` directly.

## License

MIT © Charly López — see [LICENSE](LICENSE).
