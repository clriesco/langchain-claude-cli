# Design: release-0-4

## Context

v0.3.1 publicada; 88 tests deterministas (cassettes) + contrato nightly. `chat_models.py` ~1.400 líneas.

## Goals / Non-Goals

**Goals:** módulos <500 líneas con una responsabilidad cada uno; pool testeable sin CLI; interrupt universal. **Non-Goals:** cambios de API o comportamiento en el split; replay del protocolo de control MCP del cliente (solo query/receive_response/interrupt).

## Decisions

### D1 — Split por mixins, no por funciones sueltas

`ChatClaudeCli(_OptionsMixin, _RunnerMixin, _StreamingMixin, BaseChatModel)`. Los mixins son clases planas solo-métodos (los campos pydantic permanecen en la clase principal — pydantic v2 lo permite); tipado de `self` vía anotación explícita con import bajo `TYPE_CHECKING` (sin ciclo en runtime). Alternativa descartada: funciones module-level con el modelo como parámetro — diff mayor y peor cohesión con `self`.

- `_options.py`: `_build_options`, `_translate_mcp_servers`, `_build_sdk_tools`, `_build_defer_hooks`, `_options_sig`, `_file_resolver` + helpers module-level (`_lc_tool_to_anthropic`, `_is_server_tool`).
- `_runner.py`: `_arun_query`, `_build_chat_result`, `_resolve_session`, `_build_prompt_entries`, `_agenerate`, `_generate`, `_effective_inactivity` + `_run_sync`, `_apply_stop_and_max_tokens`.
- `_streaming.py`: `_astream`, `_stream`.
- Criterio de verificación: suites unit+cassette idénticas en verde, mypy strict, y un smoke live.

### D2 — FakeClaudeSDKClient en el harness

Fixture `client_cassette`: monkeypatch de `claude_agent_sdk.ClaudeSDKClient` con un doble que reproduce exchanges grabados (`connect` no-op, `query()` consume exchange, `receive_response()` reproduce mensajes, `interrupt()` marca flag y trunca). Grabación envolviendo el cliente real igual que con `query`. Los tests del pool (fast-path, warm-up, degradación por firma/TTL) migran a esto.

### D3 — Interrupt stateless por cancelación de task

Registro por instancia de runs activos: `session-or-invoke-id → (loop, task)` poblado al entrar en `_arun_query`/`_astream` y limpiado al salir. `interrupt(session_id=None)`: con pool y cliente vivo → camino actual; si no → cancela el/los task(s) activos vía `loop.call_soon_threadsafe(task.cancel)`. El invoke cancelado lanza `ClaudeCliInterruptedError` (nueva, subclase de `ClaudeCliError`); el `finally`+`aclose` existente garantiza cierre del subproceso. Semántica con concurrencia: sin argumento cancela TODOS los runs activos de la instancia (documentado); con `session_id` solo el de esa sesión si es determinable.

## Risks / Trade-offs

- **[Mixins vs pydantic]** Métodos en mixins que acceden a campos pydantic — mypy validado con anotación de self; riesgo bajo, verificado por la suite.
- **[Cancel cruzando hilos]** `call_soon_threadsafe` + task.cancel es el único punto delicado; test dedicado con fake colgado (patrón del watchdog).

## Migration Plan

Sin migración. Release 0.4.0.

## Open Questions

- ¿Exponer también `ClaudeCliInterruptedError` como resultado parcial (contenido acumulado hasta el corte)? Decidir en implementación de streaming.
