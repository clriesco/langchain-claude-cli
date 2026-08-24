# Changelog

## 1.2.1 — 2026-08-24

### Fixed

- **Tool-calling cycles died on the second turn against Claude Code 2.1.235+.**
  A tool cycle's resume suffix is often *only* tool results, and those ride the
  MCP handlers rather than the prompt — so the turn went out with an **empty
  prompt stream**. The CLI accepted that through 2.1.234 and re-fired the
  pending call; from 2.1.235 it exits non-zero with an **empty stderr**, so the
  SDK raises an opaque `ProcessError` ("Check stderr output for details" —
  there are none) and the in-flight control request is orphaned
  (`ProcessTransport is not ready for writing`).

  Measured against 2.1.241: every `bind_tools` cycle on a resumed session died
  at its second turn. A resume that would otherwise carry nothing now sends a
  minimal continuation turn; the CLI re-fires the pending call on its own and
  the handler delivers the stored result, exactly as before.

  Found by the nightly contract suite, which had been red since the 2026-08-19
  run — green on CLI 2.1.233 and 2.1.234, red from 2.1.235. That is what the
  suite is for, and the failure was a real break in the library rather than a
  stale test: the same empty prompt the contract test sent by hand is the one
  the tool path sent in production.

## 1.2.0 — 2026-08-22

A minor, not a patch: it raises the `claude-agent-sdk` floor (see Changed).

### Fixed

- **`CLIJSONDecodeError` and `MessageParseError` lost their own type when
  wrapped.** 1.1.0 promised that every wrapper "inherits from both
  `ClaudeCliError` and the SDK class it replaces". That held for
  `CLIConnectionError`, `CLINotFoundError` and `ProcessError`, but the other
  two fell through to the generic `ClaudeCliTransportError`, which does *not*
  subclass them — so `except CLIJSONDecodeError` quietly stopped catching them.

  Measured downstream: a consumer whose retry policy listed `CLIJSONDecodeError`
  among the failures **not** worth retrying saw it fall past that branch into a
  residual `RuntimeError` one, flipping the decision from "give up" to "retry".
  Exactly the class of silent behaviour change this taxonomy exists to prevent,
  introduced by the change meant to prevent it.

  There are now `ClaudeCliJSONDecodeError` (keeps `line`, `original_error`) and
  `ClaudeCliMessageParseError` (keeps `data`), and every wrapper preserves its
  SDK type. A new test enumerates the SDK's error classes **at runtime** and
  fails if any one of them wraps into something that is not an instance of
  itself — so a class added upstream cannot slip through the same gap again.

### Changed

- **Requires `claude-agent-sdk>=0.2.144`** (was `>=0.2.115`). That release types
  the CLI's error result itself, as `ResultError(ProcessError)` carrying
  `subtype`, `errors`, `terminal_reason` and the raw payload — the very thing
  1.1.0 added `ClaudeCliResultError` for. Supporting both shapes would mean a
  conditional base class and two different MROs depending on what happened to be
  installed, which is worse than asking for a version everyone already gets on a
  fresh install. Upgrade with your usual sync; nothing else changes for you.

### Added

- **`ClaudeCliResultError` now extends the SDK's `ResultError`.** So
  `except ResultError` and `except ProcessError` catch a `ClaudeCliMaxTurnsError`
  too, and the SDK's own structured fields ride along with it.
- **The subtype is read from the SDK before the prose.** `classify_result_error`
  now prefers the captured `ResultMessage`, then `ResultError.subtype`, and only
  then falls back to matching the message text. The prose match is the last
  resort rather than the second one.

## 1.1.1 — 2026-08-22

### Added

- **`usage_metadata` on errors, next to the raw `usage`.** 1.1.0 attached the
  CLI's own usage dict to the exception, unconverted. That left every consumer
  reimplementing the conversion — and walking into a trap while doing it: the
  key `input_tokens` exists in **both** shapes and means different things. The
  CLI follows Anthropic's convention, where it counts only the uncached tokens
  and `cache_read_input_tokens` sits beside it; LangChain's `usage_metadata`
  aggregates the three and breaks them out under `input_token_details`. For one
  measured agentic run that is 1.520 against 211.520 under the same key.

  Anyone adding failures to the same token counter as successes was therefore
  undercounting exactly the runs that read the most cache. `exc.usage_metadata`
  is now the same shape `AIMessage.usage_metadata` uses on the success path —
  there is a test asserting the two are identical for the same usage — and
  `exc.usage` still carries the CLI's dict untouched for anyone who wants it.
  Both are `None` when the run never produced a result.

## 1.1.0 — 2026-08-22

Four fixes from a production batch run: ~4.100 sites through an agentic loop
(`builtin_tools=NETWORK_TOOLS`, `max_turns=25`, subscription auth, concurrency
4). All four are additive — no existing name, signature or metadata key moved.

### Added

- **Errors carry the cost of the run that failed.** When the CLI reported a
  `ResultMessage` before failing, the exception now exposes `usage`,
  `total_cost_usd`, `num_turns`, `duration_ms`, `session_id`, `subtype` and the
  raw `result_message` (each `None` when unknown, on every `ClaudeCliError`, and
  preserved across pickling). Previously the `ResultMessage` was in scope at the
  raise site and dropped, so the most expensive failures — the ones that burn
  the whole turn budget — were unaccountable from outside: a measured batch of
  50 declared 13,14 USD against ~16 actually spent, because 8 of 53 invocations
  (15%) ended this way.

  *You can stop:* reconstructing the cost of failed runs, or writing them off.

- **`ClaudeCliMaxTurnsError` and `ClaudeCliExecutionError`.** Exhausting
  `max_turns` is an expected outcome and now has a type, with the turn count on
  `.num_turns`. Both derive from the new `ClaudeCliResultError` ("the CLI ran
  and reported failure" — as opposed to a transport failure, and never
  retried). The type comes from the `ResultMessage` **subtype**
  (`error_max_turns` / `error_during_execution`), not from the CLI's wording;
  the SDK's message text is preserved verbatim as the exception message, so
  code still matching on that prose keeps working while it migrates. Both paths
  are covered — `invoke` and `stream`.

  *You can stop:* regex-matching `"Reached maximum number of turns (N)"` on an
  untyped `Exception`.

- **SDK errors arrive inside this package's hierarchy.** `CLIConnectionError`
  used to cross the wrapper unwrapped (MRO: `CLIConnectionError →
  ClaudeSDKError → Exception`), so `except ClaudeCliError` never saw it — and a
  burst of `Failed to start Claude Code: [Errno 24] Too many open files` was
  classified as a data failure instead of infrastructure, sending sites that
  were never visited to human review. Now every error raised out of this package
  is a `ClaudeCliError`:

  | SDK class | raised as |
  |---|---|
  | `CLIConnectionError` | `ClaudeCliStartupError` |
  | `CLINotFoundError` | `ClaudeCliNotFoundError` |
  | `ProcessError` | `ClaudeCliProcessError` (keeps `exit_code`, `stderr`) |
  | other `ClaudeSDKError` | `ClaudeCliTransportError` |

  Each inherits from **both** `ClaudeCliError` and the SDK class it replaces, so
  an existing `except CLIConnectionError` / `except ProcessError` still catches
  it. The original is kept as `__cause__` and `.sdk_error`.

  *You can stop:* comparing `type(exc).__name__` against a string.

- **`ChatClaudeCli.aclose()` / `close()`, and context-manager support.**
  `persistent=True` builds a `ClientPool` per instance, which nothing could
  release: one background loop thread and up to `pool_max_clients` live `claude`
  subprocesses (~700 descriptors each), held for the life of the process — and
  held by the interpreter's exit hook even after the model was dropped.
  Building one instance per unit of work exhausted the file-descriptor budget at
  the 45th. `aclose()` (and `close()`, `async with`, `with`) disconnects the
  clients, blocks until the subprocesses are actually gone, and stops the loop
  thread. Idempotent, and a no-op without `persistent=True`.

  Also documented: the pool is **per instance**, not per process — the natural
  reading of `pool_max_clients: 4` is "four clients in total", and it is four
  *per model you construct*.

### Compatibility

Verified by running the same consumer probe against 1.0.0 and 1.1.0 (four
failure modes × the catch patterns a consumer may have written). **No pattern
that caught an error before stops catching it**, and `str(exc)` is byte-for-byte
identical in every case. Two observable things do change, both by design:

- `except ClaudeCliError` now catches **strictly more**: CLI error results
  (`error_max_turns`, `error_during_execution`) and SDK errors
  (`CLIConnectionError`, `ProcessError`, ...) join it. That is the point of the
  release, but if you have a `try/except ClaudeCliError` followed by a broader
  `except Exception` doing something different, those errors move from the
  second branch to the first. Check any place where the two branches disagree.
- `type(exc).__name__` and `isinstance(exc, RuntimeError)` change for the newly
  typed errors (`"Exception"` → `"ClaudeCliMaxTurnsError"`,
  `"CLIConnectionError"` → `"ClaudeCliStartupError"`, ...). Giving these errors a
  type is what was asked for, so this is unavoidable; matching on the class name
  string is exactly what the new types replace.

`except CLIConnectionError`, `except ProcessError`, `except Exception`, every
`response_metadata` key (`num_turns`, `total_cost_usd`, `duration_ms`,
`rate_limit`) and `usage_metadata["input_token_details"]["cache_read"]` are
unaffected.

### Fixed

- **An `AssertionError` with no message under descriptor exhaustion.** The pool
  started its background loop in a thread and then asserted the loop existed
  (`assert self._loop is not None`). Creating an event loop needs file
  descriptors, so under exhaustion the thread died in `new_event_loop()`, the
  waiter timed out, and the run failed with a bare `AssertionError` —
  indistinguishable from a bug in the caller, and seen twice in the measured
  batch after 133 s and 178 s of normal work. That path now raises
  `ClaudeCliStartupError` naming the real cause, with the `OSError` as
  `__cause__`.

## 1.0.0 — 2026-07-26

First stable release. The public surface has been stable across the 0.4.x line
and is now committed to under semantic versioning.

### Added
- **`SessionStoreBackend`, `InMemoryStore` and `FileStore` are public.**
  `session_store` has always accepted a custom backend instance, but the
  protocol it had to satisfy lived in the private `_sessions` module — an API
  you could use but not import. Implementing a Redis-backed store, or pointing
  `FileStore` at a path other than the default, no longer reaches into a
  private module. The protocol itself is unchanged (`get`/`set`/`keys`/
  `delete`); only its visibility is.

### Fixed
- **An interrupted turn hijacked the rest of its conversation.** `interrupt()`
  cancels the run, but the CLI session is left holding an unfinished assistant
  reply — and resuming it makes the CLI continue *that* reply instead of
  answering the next message. Interrupting a long generation and then asking
  something else returned the tail of the abandoned answer, on every subsequent
  turn. `interrupt()` now invalidates the affected session mappings (and evicts
  the pooled client, whose stream holds the same tail), so the next turn opens
  a fresh session from the caller's history — the degrade path a purged session
  already took. Behavior change worth knowing: after an interrupt a
  conversation no longer resumes its CLI session, so that turn re-sends its
  history and loses the session's prompt cache.
- **pydantic v1 schemas returned raw dicts.** `with_structured_output()` tested
  `issubclass(schema, BaseModel)` against the pydantic **v2** base, so a v1
  model silently took the dict path and the caller got a `dict` where it asked
  for an instance. Detection now goes through langchain-core's
  `is_basemodel_subclass`, with `schema()`/`parse_obj()` on the v1 side —
  ChatAnthropic parity restored.
- **Streaming dropped structured output.** `with_structured_output()` reads
  `additional_kwargs["structured_output"]`, which the invoke path sets from the
  `ResultMessage` but the streaming path never attached. The parser then fell
  back to reading text — and a structured run emits none (its content is
  thinking + tool_use) — so `.stream()` died with
  `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`. The final chunk
  now carries it, at parity with invoke.
- **langchain-core v1 image blocks killed the CLI.** The v1 `ImageContentBlock`
  carries its payload on the item (`base64` + `mime_type`, `url` or `file_id`)
  rather than in a nested `source`, and was forwarded unchanged — reaching the
  CLI sourceless and crashing it with `undefined is not an object (evaluating
  'e.source.type')`. All three v1 forms are now normalized to Anthropic image
  blocks. The v1 `file` block for PDFs was already handled; images had been
  missed.
- **`ls_structured_output_format` reported the wrong schema.** The tracing
  metadata carried a `{name, schema}` wrapper, but langchain-core re-runs
  `convert_to_json_schema` over the value, collapsing that wrapper to just
  `{"title": <name>}` — so LangSmith saw a schema with no properties. It now
  carries the canonical JSON schema. The schema sent to the CLI is unchanged.

### Changed
- `Development Status` classifier promoted to `5 - Production/Stable`.
- README rewritten: task-oriented structure, a full options reference, and a
  headless-deployment section covering `claude setup-token` /
  `CLAUDE_CODE_OAUTH_TOKEN`. Release notes live here, not in the README.

## 0.4.3 — 2026-07-21

### Fixed
- **A purged CLI session no longer bricks its conversation.** The session store maps history fingerprints and thread keys to CLI `session_id`s, and with `session_store="file"` those mappings outlive the transcripts themselves — the CLI prunes inactive sessions after `cleanupPeriodDays` (~30 days). Resuming a purged session made the CLI exit 1 (`No conversation found with session ID`), the bridge retried the doomed resume until the budget ran out, surfaced `ProcessError`, and — worst of all — left the poisoned mapping in the store, so **every** subsequent turn of that conversation failed the same way until the file was deleted by hand. Now the failure is detected on the first attempt (before retry accounting, same spirit as the 0.4.1 contradictory-result handling), every store entry resolving to the purged session is invalidated, and the invoke transparently re-runs as a new session via the existing full-history flatten path — with its retry budget intact. The new session is registered as usual, so the next turn resumes it. Covers both invoke and streaming; a pooled (persistent) client hitting a purged session already fell back to the stateless path, where the new detection applies.
- An explicitly pinned session (`ChatClaudeCli(session_id=...)`) deliberately does **not** degrade: the caller asked for that exact session, and silently swapping in an empty one would drop context without warning. The error propagates — now immediately, without burning retries on a resume that can only fail.

### Changed
- Runs that resume a session now register the SDK's `stderr` callback (bounded capture, re-emitted at DEBUG on the `langchain_claude_cli` logger). The purge marker is only observable there: the SDK's `ProcessError` carries the placeholder "Check stderr output for details" instead of the real stderr. Side effect: on those runs the CLI's stderr no longer passes through to the parent process's stderr.

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
