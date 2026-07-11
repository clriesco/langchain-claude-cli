# Tasks: release-0-3

## 1. Cassettes (tests deterministas)

- [x] 1.1 Serializador de mensajes SDK: dataclass ↔ dict con discriminador `__type__` (AssistantMessage+blocks, ResultMessage, StreamEvent, RateLimitEvent) + tests unitarios de round-trip
- [x] 1.2 Harness `tests/_cassettes.py`: fixture `cassette` (replay con matching laxo model/entries/tools) + modo `RECORD_CASSETTES=1` (delega en query real y vuelca)
- [x] 1.3 Grabar cassettes de los flujos núcleo: invoke, multiturn resume, ciclo tool calling (2 llamadas), paralelas, structured output, streaming, stop-seq, PDF
- [x] 1.4 Migrar la suite E2E a cassettes por defecto (`-m live` conserva la ejecución real); CI ejecuta la suite con cassettes en cada push
- [x] 1.5 Verificar suite cassette completa en verde sin CLI (simular con PATH sin `claude`)

## 2. Watchdog + logging

- [x] 2.1 S10 — spike: gap máximo real entre mensajes SDK en generación larga sin partial messages (sonnet + thinking) → fija default pure-LLM
- [x] 2.2 `inactivity_timeout` en `_collect2` y `_astream` (wait_for por mensaje + aclose + `ClaudeCliError`); defaults según spike (agéntico: None)
- [x] 2.3 Logger `langchain_claude_cli` con NullHandler; eventos DEBUG (sesión, pool, defer/delivery) e INFO/WARNING (reintentos, watchdog, degradaciones)
- [x] 2.4 Tests: watchdog con cassette de stream truncado (unit, sin CLI) + verificación de no-huérfanos; logging capturado con caplog
- [x] 2.5 Reportar issue upstream en claude-agent-sdk (stream que no termina al morir el CLI) con repro mínimo

## 3. Tests de contrato nightly

- [x] 3.1 `tests/contract_tests/` con los invariantes S1-S9 como tests pytest marker `contract` (coste acotado: haiku + max_budget_usd por test)
- [x] 3.2 Workflow `contract.yml`: cron nightly + workflow_dispatch; instala CLI, secret `ANTHROPIC_API_KEY` + `auth="inherit"`; skip amable sin secret
- [x] 3.3 Ejecutar la suite de contrato en local (OAuth) una vez completa — verde antes de confiar en el nightly

## 4. Release

- [ ] 4.1 mypy + ruff + suite completa (cassettes + unit) en verde; README (sección testing/watchdog/logging) + CHANGELOG 0.3.0
- [ ] 4.2 Bump 0.3.0, tag `v0.3.0`, push; verificar publicación y nightly manual (`workflow_dispatch`) del contrato
