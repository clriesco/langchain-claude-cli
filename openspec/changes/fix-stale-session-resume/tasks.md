# Tasks — fix-stale-session-resume

## 1. Red de seguridad

- [x] 1.1 Baseline verde de `tests/unit_tests`, `tests/contract_tests`,
      `tests/cassette_tests` antes de tocar nada (106 passed, 2 skipped)
- [x] 1.2 Reproducir el bug contra el CLI real (mapeo envenenado desde un nodo
      LangGraph → `ProcessError`, con 3 resumes condenados: 1 + 2 reintentos)

## 2. Invalidación en el store (D4)

- [x] 2.1 `SessionCache.invalidate(session_id)`: elimina toda entrada `fp:` y
      `thread:` que resuelva a ese `session_id` y las purga de la lista LRU
- [x] 2.2 Test unitario: invalida hermanos (varios `fp:` + `thread:`),
      preserva entradas ajenas y deja la LRU consistente

## 3. Detección y degradación en invoke (D1, D2, D3)

- [x] 3.1 Colector de stderr acotado registrado en `options.stderr` para runs
      con `strategy="resume"`, reemitido a DEBUG
- [x] 3.2 Detector `_is_stale_session_error(exc, stderr_lines)` (marcador en
      `str(exc)`, `exc.stderr` o búfer)
- [x] 3.3 `Resolution.explicit` y bucle de rondas de sesión en
      `_arun_query_inner`: invalidar + reejecutar como `new` sin consumir
      reintentos; `explicit` propaga inmediatamente
- [x] 3.4 Tests con doble de `query` (sin CLI): degradación transparente,
      presupuesto de reintentos intacto (2 llamadas exactas), store saneado y
      sesión nueva registrada, propagación con `session_id` explícito

## 4. Streaming (D5)

- [x] 4.1 Mismo bucle de rondas en `_astream`, con guarda de "ningún chunk
      emitido aún"
- [x] 4.2 Test con doble de `query`: `stream()` degrada y emite los chunks de
      la sesión nueva

## 5. Verificación

- [x] 5.1 Suite completa verde: 114 passed, 2 skipped (106 baseline + 8 nuevos)
- [x] 5.2 `ruff check`, `ruff format --check` y `mypy` limpios
- [x] 5.3 Prueba end-to-end real contra el CLI: el repro del bug pasa a
      responder (turno 1 degrada con warning de flatten) y el segundo invoke
      reanuda la sesión nueva (mismo `session_id` en ambos turnos)
- [x] 5.4 CHANGELOG + bump a 0.4.3

## 6. Entrega

- [x] 6.1 Rama `fix/stale-session-resume` + push + PR contra `main`
- [x] 6.2 Publicado 0.4.3 a PyPI (tag v0.4.3, workflow Release verde)
