# Design: release-0-3

## Context

v0.2.1 publicada. Todos los caminos de generación pasan por `claude_agent_sdk.query()` (el pool persistente usa `ClaudeSDKClient` — fuera del alcance del replay v1). Los mensajes del SDK son dataclasses serializables (`AssistantMessage` con blocks, `ResultMessage`, `StreamEvent` con dict raw, `RateLimitEvent`). Lecciones operativas de v0.2: cuelgue del SDK con CLI muerto (stream abierto sin fin), flakiness bajo rate limit, y dos reportes downstream reales en 24h.

## Goals / Non-Goals

**Goals:**
- Suite E2E mayoritariamente determinista, sin cuota y estable en CI.
- Ningún run puede colgar indefinidamente por defecto razonable; diagnóstico vía logging.
- Detección proactiva de cambios de comportamiento del CLI (contrato nightly).

**Non-Goals:**
- Replay del camino `ClaudeSDKClient`/pool (los tests de pool siguen unit+live smoke; v0.4).
- Refactor del monolito `chat_models.py` (pendiente, no bloquea esto).
- Arreglar el bug upstream del SDK (se reporta issue; nosotros mitigamos).

## Decisions

### D1 — Cassettes: interceptar en la frontera `query()`, serialización propia mínima

Fixture pytest `cassette` que monkeypatchea `claude_agent_sdk.query` con un doble que reproduce una lista de mensajes serializados (`tests/cassettes/<nombre>.json`). Serializador propio de ~100 líneas (dataclass → dict con discriminador `__type__`; reconstrucción con los tipos reales del SDK) — sin dependencia de VCR (diseñado para HTTP, no para streams de dataclasses).

- **Grabación**: `RECORD_CASSETTES=1 pytest -m live_record` envuelve el `query` real, colecciona los mensajes y los vuelca. El prompt/options de cada llamada se guarda como metadato para asertar que el replay recibe la misma forma de request (matching laxo: model + nº de entries + tools presentes; NO texto exacto — los prompts contienen rutas temporales).
- **Multi-llamada**: una cassette es una lista ordenada de intercambios; cada llamada a `query()` consume el siguiente (los tests de ciclo de tools hacen 2+ llamadas).
- Los tests migrados conservan una variante live: el mismo test corre con cassette por defecto y contra CLI real bajo `-m live`.

*Alternativa descartada*: fake de transporte dentro del SDK (frágil, APIs internas) o VCR/HTTP (nivel equivocado).

### D2 — Watchdog de inactividad

Nuevo parámetro `inactivity_timeout: float | None`. Implementación: en `_collect2` (y `_astream`), el `async for` se consume vía `asyncio.wait_for(anext(it), inactivity_timeout)` por mensaje; al expirar → `aclose()` del stream (patrón anti-huérfanos ya existente) + `ClaudeCliError("no SDK activity for Ns")`.

Defaults (spike S10 valida): **pure-LLM 120s** (una generación no produce silencios mayores: sin partial messages el gap máximo es un turno completo de haiku/sonnet — validar con sonnet+thinking largo) y **None (desactivado) en modo agéntico** (un `Bash` legítimo puede callar minutos). El usuario puede fijar ambos. `default_request_timeout` sigue siendo el techo total y compone con este.

Logging: logger `langchain_claude_cli` (NullHandler por defecto); eventos DEBUG (resolución de sesión, pool hit/miss, defer/delivery) e INFO/WARNING (reintentos, watchdog, degradaciones flatten/replay que hoy son warnings de compat).

### D3 — Contrato nightly con auth por API key en CI

`tests/contract_tests/` con los invariantes de los spikes S1-S9 (defer round-trip, paralelas, replay assistant, PDF, streaming shapes, effort levels, RateLimitEvent, neutralización de env). Workflow `contract.yml`: cron nightly + `workflow_dispatch`, instala CLI (`npm i -g @anthropic-ai/claude-code`), corre con `ANTHROPIC_API_KEY` del repo secret y `ChatClaudeCli(auth="inherit")` — en CI no hay OAuth; el coste (~céntimos/noche con haiku) lo paga la key del mantenedor. Si el secret no existe, el job se salta con aviso (fork-friendly).

*Alternativa descartada*: runner self-hosted con OAuth — más fiel pero más frágil y con superficie de seguridad mayor.

## Risks / Trade-offs

- **[Cassettes desactualizadas]** El CLI evoluciona y las grabaciones envejecen → el contrato nightly es exactamente el contrapeso; regrabar es un comando.
- **[Watchdog false-positive]** Turno legítimo más largo que el default → default agéntico desactivado + parámetro configurable + WARNING con el valor usado.
- **[Matching laxo de cassettes]** Un cambio de conversión podría pasar inadvertido → los tests unitarios de `_convert` cubren la forma exacta; la cassette cubre el flujo.
- **[Contrato en CI usa API key]** Facturación real → modelo haiku, presupuesto por test (`max_budget_usd`), y solo invariantes (pocas llamadas).

## Migration Plan

Sin migración de usuarios. `inactivity_timeout` con defaults no intrusivos. Release 0.3.0 vía tag.

## Open Questions

- Gap máximo real entre mensajes SDK en generación larga sin partial messages (spike S10 → fija el default pure-LLM).
- ¿`workflow_dispatch` debe poder regrabar cassettes y subirlas como artifact? (nice-to-have, no bloquea).
