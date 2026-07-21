# Changelog

## 0.4.2 — 2026-07-21

### Fixed
- **Session recovery by `thread_id` never ran.** The recovery path read `kwargs["config"]`, but `BaseChatModel.invoke/ainvoke` consume `config` as their own parameter and never forward it to `**kwargs` — and `bind(config=...)` raises `TypeError` on the positional collision. The `thread_id` is now resolved from the explicit kwarg **or**, failing that, langchain-core's ambient config (`ensure_config()`), which LangGraph populates while running a node. Practical effect: a conversation inside a LangGraph node whose checkpointer normalizes `AIMessage` content (breaking the prefix fingerprint) now resumes its CLI session instead of degrading to flatten on every turn. `SessionCache` itself was already correct — its unit tests called it directly, so the missing wiring went unnoticed.
- Only `thread_id` is read from the ambient config — deliberately **not** `session_id`. That key is overloaded in the LangChain ecosystem (`RunnableWithMessageHistory`'s default field spec uses `session_id` as a chat-history key), and honoring an ambient one would hijack the session with a value never addressed to this model. To pin a CLI session, use the constructor: `ChatClaudeCli(session_id="<uuid>")`.

### Changed
- **Thread recovery keys are namespaced by execution profile.** A LangGraph `thread_id` identifies a graph thread, not a conversation: several model instances routinely share one (e.g. a cheap router and an expensive executor). The `thread:` key now carries a digest of `model`, `cwd`, `builtin_tools` and `permission_mode`, so they cannot resume each other's session. The digest deliberately excludes `system_prompt` — runtimes recompose it every turn (date, memory, active skills) and including it would make the key volatile and disable recovery entirely. Pre-upgrade `thread:` entries are simply not found, which degrades to the previous behavior; no migration needed.

### Note
- Turns that must NOT resume (heartbeats, crons, one-shot jobs) opt out without new API: `session_store="memory"` is per-instance, so building a fresh model per turn never resumes.

## 0.4.1 — 2026-07-13

### Fixed
- A contradictory CLI result (`is_error=true` + `subtype="success"`, seen under usage-window pressure) is no longer surfaced as a fatal untyped `Exception`: the turn's already-collected assistant messages are recovered as the success the CLI reported, or — when there is nothing to recover — a typed retryable `ClaudeCliOverloadedError` is raised so retry/fallback policies apply. Genuine error results (`error_max_turns`, 4xx/5xx/529, budget exceeded) are unchanged.

## 0.4.0 — 2026-07-12

### Added
- `interrupt()` now works in any mode: stateless runs are cancelled via task cancellation and raise `ClaudeCliInterruptedError` (new), with guaranteed subprocess cleanup. Without `session_id` it cancels all active runs of the instance.
- Cassette harness covers the persistent-client path (`ClaudeSDKClient` double): the pool's warm-up/reuse flow is now tested deterministically without the CLI.

### Changed
- Internal split of `chat_models.py` (1357 → 345 lines) into focused modules (`_options`, `_runner`, `_streaming`) — pure refactor, public API unchanged.
- `interrupt()` with nothing to cancel now raises `ClaudeCliError("no active run to cancel")` instead of requiring `persistent=True`.

## 0.3.1 — 2026-07-11

### Fixed
- **Python 3.10**: `asyncio.TimeoutError` and builtin `TimeoutError` are distinct classes before 3.11 — both the inactivity watchdog (0.3.0) and the total `timeout` (latent since 0.1.0) failed to catch the timeout on 3.10, surfacing as `CancelledError` instead of `ClaudeCliTimeoutError`.
- CI: fixed venv clash with setup-uv and missing `langchain` test dependency — the matrix (3.10/3.12/3.13) is now actually green.

## 0.3.0 — 2026-07-11

### Added
- Inactivity watchdog (`inactivity_timeout`, default 120s pure-LLM / disabled agentic): a dead CLI process can no longer hang an invoke forever; aborts with `ClaudeCliTimeoutError` and cleans up the subprocess.
- Structured logging under the `langchain_claude_cli` logger (session resolution, pool, defer/delivery, retries, watchdog).
- Deterministic cassette test harness (record/replay of SDK streams) — core E2E suite runs with no CLI and no quota.
- Nightly CLI contract suite (`contract.yml`): checks the live CLI still honors the behavior invariants the library depends on.

### Changed
- `history_mode="replay"` documented as **experimental**: the CLI generates live replies to historical user messages and may prefer them over injected assistant turns (contract-suite finding).

## 0.2.1 — 2026-07-11

### Fixed
- Added the missing `py.typed` marker (PEP 561): downstream type checkers now see the package's inline types (downstream report).

## 0.2.0 — 2026-07-11

### Added
- Persistent session store (`session_store="file"`): conversations resume across process restarts; `thread_id` (LangGraph) recovery path for trimmed histories.
- Persistent client mode (`persistent=True`): live CLI client per conversation (~2× faster reused turns), `interrupt()`, `set_session_model()`, LRU+TTL pool with clean shutdown.
- Typed exception taxonomy: `ClaudeCliError`, `ClaudeCliRateLimitError`, `ClaudeCliOverloadedError`, `ClaudeCliAuthError`, `ClaudeCliTimeoutError` (plus existing `ClaudeCliBudgetExceededError`).
- OAuth guard (`auth="oauth"`, default): neutralizes inherited `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` in the CLI subprocess — subscription billing guaranteed; `auth="inherit"` opts out.
- `response_metadata["rate_limit"]`: subscription window status/type/utilization/resets_at on every response.
- `history_mode="replay"`: faithful multi-message replay of arbitrary histories (opt-in; documented cost).
- Files API blocks (`file_id`): materialized via the Anthropic API when a key is available (download only — never passed to the CLI), otherwise dropped with a warning.
- `langchain_claude_cli.middleware.ClaudeCodeToolsMiddleware`: delegate sandboxed, budget-capped Claude Code runs as a tool in any LangChain 1.x agent.
- langchain-tests standard integration suite wired with documented xfails.

### Fixed
- **Retryable API errors (429/5xx/529) with no attempts left were silently returned as empty AIMessages** instead of raising — worst with `max_retries=0`, where a single 429 produced an undetectable empty completion (downstream report). They now raise the corresponding typed exception (`ClaudeCliRateLimitError`/`ClaudeCliOverloadedError`).
- Budget exhaustion no longer consumes retries (raises `ClaudeCliBudgetExceededError` immediately); explicit CLI error results are no longer retried.
- Orphaned `claude` subprocesses after a timeout: the SDK stream is now closed inside the still-running event loop.

## 0.1.0 — 2026-07-10

Initial release: `ChatClaudeCli`, a drop-in `ChatAnthropic` replacement on the Claude Code CLI (subscription OAuth, no API key). Classic tool calling via in-process MCP + defer, native structured output, native thinking/effort, real usage metadata, session prefix-cache, token-by-token streaming (text/thinking/tool calls/agentic activity), opt-in agentic mode with sandbox and budget caps.
