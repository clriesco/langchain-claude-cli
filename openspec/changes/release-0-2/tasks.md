# Tasks: release-0-2

## 1. Spikes

- [ ] 1.1 S6 — ¿Emite el CLI `RateLimitEvent` en modo no-interactivo? Script `spikes/s6_ratelimit.py`; conclusión en `spikes/FINDINGS.md`
- [ ] 1.2 S7 — ¿Acepta el input stream-json bloques `source: {"type": "file", "file_id": ...}`? Script `spikes/s7_files_api.py`; decide passthrough vs materialización vs omisión
- [ ] 1.3 S8 — `ClaudeSDKClient`: medir latencia multi-turn reutilizado vs `query()` con resume; validar `interrupt()` y `set_model()`; conclusión en FINDINGS
- [ ] 1.4 Revisar FINDINGS y ajustar design si algún spike contradice D4/D6

## 2. Sesiones persistentes

- [ ] 2.1 Protocolo `SessionStore` mínimo (get/set/items) + `InMemoryStore` (refactor sin cambio de comportamiento) + `FileStore` (JSON, file-lock, escritura atómica, poda LRU)
- [ ] 2.2 Parámetro `session_store="memory"|"file"|instancia` en `ChatClaudeCli`; default `"memory"`
- [ ] 2.3 Keying por `thread_id` (`config.configurable.thread_id`) como recuperación cuando el prefijo no matchea
- [ ] 2.4 `history_mode="replay"`: replay fiel del historial en sesión nueva + warning de coste
- [ ] 2.5 Tests unitarios (FileStore concurrente, thread_id fallback, replay) + test de integración de supervivencia a reinicio (dos procesos)

## 3. Cliente persistente

- [ ] 3.1 `_ClientPool` thread-safe: mapa session_id→ClaudeSDKClient, LRU+TTL configurables, disconnect en evicción, cierre en atexit
- [ ] 3.2 Integración en `_arun_query`/`_astream`: reutilizar cliente vivo en resolución resume; registrar cliente al terminar run; degradación a stateless si el pool falla
- [ ] 3.3 `interrupt(session_id=None)` y `set_model()` en caliente
- [ ] 3.4 Tests de integración: latencia multi-turn (persistente < stateless), interrupt + invoke posterior, evicción, default intacto

## 4. Paridad restante

- [ ] 4.1 `RateLimitEvent` → `response_metadata["rate_limit"]` (según S6)
- [ ] 4.2 Bloques Files API en `_convert.py` (según S7): passthrough / materialización con API key / omisión con warning
- [ ] 4.3 Cablear `ChatModelIntegrationTests` de langchain-tests con xfails documentados de niveles B/C
- [ ] 4.4 Tests unitarios de 4.1-4.2
- [ ] 4.5 Taxonomía de excepciones tipadas: `ClaudeCliRateLimitError`, `ClaudeCliOverloadedError`, `ClaudeCliAuthError`, `ClaudeCliTimeoutError` mapeadas desde `api_error_status` y excepciones del SDK (para políticas de fallback tipo EC-30 sin clasificar texto) + tests
- [ ] 4.6 Blindaje OAuth: spike de si `env={"ANTHROPIC_API_KEY": ""}` la anula para el CLI; parámetro `auth="oauth"|"inherit"` (default `"oauth"` que garantiza que el subproceso no use API key) + test que verifica que con key en el entorno se factura a la suscripción

## 5. Middleware

- [ ] 5.1 Paquete `langchain_claude_cli/middleware/` con `ClaudeCodeToolsMiddleware` (import lazy de `langchain`; el paquete principal no lo requiere)
- [ ] 5.2 Manejo de límites: budget excedido → resultado de error de tool (no excepción al grafo); sandbox y builtin_tools aplicados por ejecución
- [ ] 5.3 Tests de integración: `create_agent` + middleware resuelve tarea de filesystem; presupuesto excedido no rompe el grafo; import sin langchain no falla
- [ ] 5.4 Probar el middleware en el testbed con ambos providers como orquestador

## 6. Calidad y release

- [ ] 6.1 mypy strict + ruff limpios; suite completa unit + integración en verde
- [ ] 6.2 README: secciones nuevas (persistencia, cliente persistente, replay, middleware) + actualizar matriz de paridad (rate_limit, files API)
- [ ] 6.3 CHANGELOG.md con 0.1.0 y 0.2.0
- [ ] 6.4 Bump a 0.2.0, tag `v0.2.0`, push — release automático vía Trusted Publishing; verificar en PyPI
