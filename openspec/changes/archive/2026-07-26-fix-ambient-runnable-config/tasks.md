# Tasks — fix-ambient-runnable-config

## 1. Red de seguridad

- [x] 1.1 Baseline verde de `tests/unit_tests`, `tests/contract_tests`,
      `tests/cassette_tests` antes de tocar nada (94 passed, 2 skipped)

## 2. Resolución de config ambiental (D1)

- [x] 2.1 Helper `_effective_config(config)` → `config or ensure_config()`
- [x] 2.2 `_runner.py`: usarlo únicamente en `_thread_key`; `_resolve_session`
      lee `session_id` solo del kwarg explícito o del constructor
- [x] 2.3 `_streaming.py`: hereda el arreglo vía `_thread_key` (los dos puntos de
      llamada pasan por él; no hizo falta tocar la resolución allí)
- [x] 2.4 Test unitario: el kwarg explícito gana al ambiental
- [x] 2.5 Del config ambiental NO se lee `session_id` (colisión con la clave de
      historial de `RunnableWithMessageHistory`) — test de no-secuestro en grafo
      real (review post-PR)

## 3. Namespacing de la clave de thread (D2)

- [x] 3.1 `_thread_id(config)` → `_thread_key(config)`, que devuelve la clave ya
      compuesta (`SessionCache` no cambia de firma)
- [x] 3.2 `_session_profile()` en `_options.py`: digest estable de model, cwd,
      builtin_tools, permission_mode — NO `system_prompt`
- [x] 3.3 Cablearlo en los puntos de llamada de `_runner.py` y `_streaming.py`
- [x] 3.4 Test unitario: dos perfiles sobre el mismo `thread_id` no se cruzan
- [x] 3.5 Test unitario: mismo perfil con `system_prompt` distinto SÍ reanuda

## 4. Contract test en grafo real (D5)

- [x] 4.1 Test que compila un `StateGraph`, invoca desde un nodo con `thread_id`
      en el config y afirma que la clave de hilo es alcanzable (sin CLI)
- [x] 4.2 Test de regresión: fuera de un runnable, sin config, sigue siendo `None`

## 5. Verificación

- [x] 5.1 Suite completa verde: 105 passed, 2 skipped (94 baseline + 11 nuevos)
- [x] 5.2 `ruff check` y `mypy` limpios
- [x] 5.3 Prueba end-to-end real contra el CLI: dos turnos con `session_store`
      persistente desde un nodo LangGraph, `system_prompt` distinto por turno e
      historial normalizado a `str` → sin flatten y con memoria del turno 1
      (recuperó el código `PELICANO-77` sin reenviarlo)
- [x] 5.4 README: nota sobre el opt-out `session_store="memory"`
- [x] 5.5 CHANGELOG + bump a 0.4.2

## 6. Entrega

- [x] 6.1 Rama `fix/ambient-runnable-config` + push
- [x] 6.2 Publicado 0.4.2 a PyPI (tag v0.4.2, workflow Release verde)
