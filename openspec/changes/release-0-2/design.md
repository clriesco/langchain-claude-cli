# Design: release-0-2

## Context

v0.1 publicada (PyPI 0.1.0, 46 unit + 21 integration tests, mypy strict). Base verificada: `query()` stateless por invoke + prefix-cache en memoria (D2/D4 de v0.1). El SDK 0.2.115 ofrece `ClaudeSDKClient` (connect/query/receive_response/interrupt/set_model), `RateLimitEvent` (dataclass con `status`, `rate_limit_type`, `utilization`, `resets_at`), y el spike S3 demostró que el replay multi-mensaje funciona con coste O(turnos user históricos).

## Goals / Non-Goals

**Goals:**
- Conversaciones que sobreviven reinicios sin pérdida de fidelidad (cache persistente).
- Latencia multi-turn reducida y control del run (persistent client, opt-in).
- Tools nativas del CLI disponibles en `create_agent` vía middleware estándar.
- Cerrar deuda de paridad: Files API, rate limits, suite oficial.
- Cero breaking changes.

**Non-Goals:**
- Exponer subagentes/skills/plugins/hooks de usuario (bloque descartado del alcance).
- Session store remoto (el protocolo `SessionStore` del SDK queda para v0.3).
- Compartir un mismo cliente persistente entre conversaciones distintas.

## Decisions

### D1 — Cache persistente: backend pluggable con default JSON-en-disco

`SessionCache` gana un parámetro `store` con protocolo mínimo (`get/set/items`) y dos implementaciones: `InMemoryStore` (default actual, sin cambios de comportamiento) y `FileStore` (JSON con file-locking en `~/.langchain-claude-cli/sessions.json`, escritura atómica, poda LRU al mismo `maxsize`). Se activa con `ChatClaudeCli(session_store="file")` o pasando una instancia. El fingerprint no cambia (estable entre procesos por diseño: sha256 de contenido normalizado).

*Alternativa descartada*: sqlite — más robusto pero sobredimensionado para un mapa fingerprint→uuid; reevaluar si aparecen problemas de contención.

### D2 — Keying adicional por `thread_id`

Cuando el invoke llega con `config.configurable.thread_id` (patrón LangGraph checkpointer), se registra también `thread_id → session_id` como atajo: si el prefijo no matchea (p.ej. el checkpointer recorta historial) pero el thread es conocido, se reanuda su sesión enviando el sufijo posterior al último mensaje conocido de esa sesión; si no es determinable, flatten como hoy. El prefix-cache sigue siendo la vía principal; el thread_id es red de seguridad.

### D3 — `history_mode="replay"`

Tercera opción del literal existente. En resolución `new` con historial arbitrario, en vez de flatten se reproduce el historial completo como stream de mensajes user/assistant (mecánica ya soportada por `_convert.entries`). Warning único documentando el coste (una generación por mensaje user histórico). Sin cambios en `resume`.

### D4 — Cliente persistente: pool por conversación, opt-in

`ChatClaudeCli(persistent=True)` activa un `_ClientPool` (privado, thread-safe): mapa `session_id → ClaudeSDKClient` con TTL (default 300s) y máximo N clientes (default 4); evicción cierra con `disconnect()`. El flujo de invoke: si la resolución es `resume` y hay cliente vivo para esa sesión → `client.query(sufijo)` + `receive_response()` (sin re-arranque); si no → camino v0.1 (`query()`) y se crea/registra cliente al terminar. Métodos nuevos en el modelo: `interrupt(session_id=None)` (cancela el run activo) y `set_model()` en caliente sobre el cliente de la conversación. `__del__`/`atexit` cierran el pool best-effort.

*Riesgo asumido*: un `ClaudeSDKClient` es una sesión CLI interactiva; si el proceso muere sin cierre, el subproceso huérfano expira solo (comportamiento del CLI). Documentado.

### D5 — Middleware: `ClaudeCodeToolsMiddleware` sobre el modo agéntico existente

No reimplementa tools: envuelve un `ChatClaudeCli` agéntico como *tool del agente*. El middleware registra una tool `claude_code(task: str)` (nombre configurable) cuyo handler ejecuta un run agéntico (`builtin_tools`, `cwd`, `sandbox`, `max_budget_usd` del constructor del middleware) y devuelve el resultado final. Así cualquier modelo del agente (incluso no-Claude) puede delegar trabajo de filesystem/bash sandboxed a Claude Code, manteniendo el loop clásico del grafo.

*Alternativa descartada*: exponer Bash/Edit/Read como tools individuales ejecutadas por el middleware (estilo langchain-anthropic) — duplicaría la implementación local de tools que el CLI ya tiene mejor hecha, y perdería permisos/sandbox nativos.

### D6 — RateLimitEvent y Files API

- `RateLimitEvent` llega en el stream del SDK: se captura el último y se mapea a `response_metadata["rate_limit"] = {status, type, utilization, resets_at}`. Si el CLI no lo emite en modo no-interactivo (open question de v0.1), la clave simplemente no aparece — sin fallo. Spike S6 lo determina.
- Files API: spike S7 prueba si el input stream-json acepta `source: {"type": "file", "file_id": ...}`. Si no, workaround: cliente anthropic opcional para descargar el archivo si hay API key; sin key → `ClaudeCliCompatWarning` + bloque omitido. Nivel B documentado.

## Risks / Trade-offs

- **[FileStore compartido entre procesos]** Escrituras concurrentes → file lock + escritura atómica (rename); el peor caso es perder un registro reciente, que degrada a flatten (no corrompe).
- **[Clientes persistentes zombis]** TTL corto por defecto + cierre en evicción/atexit; documentar que `persistent=True` es para procesos de vida controlada.
- **[Interrupt a mitad de tool call]** El estado de la sesión CLI puede quedar con tool_use pendiente; el siguiente resume lo re-dispara (mecánica defer ya conocida). Test de integración lo cubre.
- **[Middleware con modelos no-Claude]** La tool `claude_code` consume cuota de la suscripción aunque el modelo orquestador sea otro; documentar en README.

## Migration Plan

Sin migración: todo opt-in, defaults intactos. Release 0.2.0 vía tag `v0.2.0` (Trusted Publishing ya configurado).

## Open Questions

- ¿Emite el CLI `RateLimitEvent` en modo no-interactivo? (spike S6; si no, la feature queda dormida sin coste).
- ¿Acepta el input stream-json bloques `file_id`? (spike S7).
- TTL/tamaño de pool óptimos — defaults conservadores y configurables, medir en el testbed.
