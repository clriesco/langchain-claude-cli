# Changelog

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
- Budget exhaustion no longer consumes retries (raises `ClaudeCliBudgetExceededError` immediately); explicit CLI error results are no longer retried.
- Orphaned `claude` subprocesses after a timeout: the SDK stream is now closed inside the still-running event loop.

## 0.1.0 — 2026-07-10

Initial release: `ChatClaudeCli`, a drop-in `ChatAnthropic` replacement on the Claude Code CLI (subscription OAuth, no API key). Classic tool calling via in-process MCP + defer, native structured output, native thinking/effort, real usage metadata, session prefix-cache, token-by-token streaming (text/thinking/tool calls/agentic activity), opt-in agentic mode with sandbox and budget caps.
