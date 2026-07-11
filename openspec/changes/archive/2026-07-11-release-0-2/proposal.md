# Proposal: release-0-2

## Why

v0.1 logró la paridad drop-in con `ChatAnthropic` y está publicada en PyPI, pero dejó tres límites conocidos: (1) el prefix-cache de sesiones es en-memoria y por instancia — un reinicio del proceso degrada cualquier conversación existente a *structured flatten*; (2) cada invoke arranca un subproceso del CLI (~1-3s), un coste evitable en conversaciones multi-turn intensivas; (3) los agentes `create_agent` de LangChain 1.x no pueden aprovechar las capacidades nativas de Claude Code (bash/editor sandboxed) sin salirse del patrón clásico. v0.2 ataca los tres, más la deuda menor de paridad (Files API, RateLimitEvent, suite oficial de integración).

## What Changes

- **Robustez core**:
  - Prefix-cache de sesiones **persistente** (backend pluggable con default en disco, keyed también por `thread_id` de LangGraph cuando exista) — las conversaciones sobreviven reinicios del proceso con fidelidad completa.
  - `history_mode="replay"` opt-in (validado en spike S3 de v0.1): replay fiel de historial arbitrario asumiendo su coste, documentado.
  - `RateLimitEvent` del SDK expuesto en `response_metadata["rate_limit"]`.
  - Files API de ChatAnthropic: bloques con `file_id` → descarga/materialización local o passthrough según soporte del CLI (spike).
  - Suite oficial `ChatModelIntegrationTests` de langchain-tests cableada con xfails documentados.
- **Cliente persistente** (opt-in): `ChatClaudeCli(persistent=True)` mantiene un `ClaudeSDKClient` vivo por conversación — sin re-arranque de subproceso en multi-turn, `interrupt()` para cancelar generaciones, `set_model()` en caliente. Lifecycle gestionado (cierre por LRU/TTL y al destruir la instancia).
- **Middleware para `create_agent`**: paquete `langchain_claude_cli.middleware` con `ClaudeCodeToolsMiddleware` — inyecta tools nativas de Claude Code (Bash/Edit/Read sandboxed, con permisos y presupuesto del CLI) en cualquier agente LangChain 1.x, el análogo inverso del middleware de langchain-anthropic.
- Sin breaking changes: todo opt-in; los defaults de v0.1 se mantienen.

## Capabilities

### New Capabilities

- `persistent-client`: modo cliente persistente — lifecycle del `ClaudeSDKClient`, interrupt, cambio de modelo en caliente, y garantías de limpieza.
- `agent-middleware`: middleware `create_agent` que expone las tools nativas del CLI al loop del agente LangChain.

### Modified Capabilities

- `session-management`: el prefix-cache pasa a ser persistente y pluggable; se añade `history_mode="replay"`; keying por `thread_id`.
- `chat-model-core`: `response_metadata` incorpora `rate_limit`; la conversión de mensajes soporta bloques Files API (`file_id`).

## Impact

- **Código**: `_sessions.py` (backend de persistencia), `chat_models.py` (modo persistente, rate limit, files), nuevo `middleware/`. Tests unit + integración para todo lo nuevo.
- **Dependencias**: sin nuevas obligatorias; el backend de disco usa stdlib.
- **Compatibilidad**: API v0.1 intacta; version bump a 0.2.0 con release vía Trusted Publishing (tag `v0.2.0`).
