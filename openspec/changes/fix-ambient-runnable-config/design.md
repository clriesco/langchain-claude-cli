# Design — fix-ambient-runnable-config

## D1. De dónde sale el `RunnableConfig`

`BaseChatModel.ainvoke(input, config=None, *, stop=None, **kwargs)` descompone
`config` en `callbacks`/`tags`/`metadata`/`run_name`/`run_id` y lo pierde. No hay
forma pública de meterlo en `**kwargs`: `bind(config=...)` colisiona con el
parámetro posicional.

Lo que sí sobrevive es el contextvar `var_child_runnable_config`, que
`ensure_config()` lee y que **LangGraph puebla** al ejecutar un nodo. Sonda
medida (ver proposal): dentro de un nodo, `ensure_config()` devuelve el
`thread_id`; `kwargs["config"]` sigue vacío.

Regla adoptada:

```
config_efectivo = kwargs.get("config") or ensure_config()
```

El kwarg explícito primero (compatibilidad hacia atrás y control del llamante),
el ambiental como respaldo. `ensure_config()` siempre devuelve un dict, así que
el resto del código no cambia de forma.

**Alternativa descartada** — añadir kwargs bindables `thread_id=` /
`session_id=` (`llm.bind(thread_id=...)`). Funcionaría, pero obliga a cada
llamante a cablearlo a mano justo cuando el dato ya está disponible, y deja las
dos vías vivas. Se puede añadir después si aparece un caso fuera de runnable;
no es necesario para cumplir la spec.

**Coste**: `ensure_config()` construye un dict por llamada. Es una vez por
invoke, frente a un subproceso del CLI. Irrelevante.

## D2. Namespacing de la clave de thread

En cuanto D1 funciona, `thread_id` deja de ser un identificador de conversación
para pasar a ser un identificador de **hilo del grafo**, y en un grafo real
varias instancias del modelo comparten hilo. Caso concreto del consumidor
`locamala-mesh`: sobre `paco:123456` corren un clasificador (haiku, `max_turns=1`,
sin tools) y un ejecutor (sonnet, con tools). Sin namespacing, el clasificador
resolvería por `thread:paco:123456` y reanudaría la sesión del ejecutor.

```
        HOY                          PROPUESTO
  thread:<id>                  thread:<perfil>:<id>
                               perfil = sha256(model, cwd,
                                               builtin_tools,
                                               permission_mode)[:16]
```

**Dónde vive el namespacing** — en el modelo, no en `SessionCache`. El método
privado `_thread_id(config)` pasa a `_thread_key(config)` y devuelve ya la clave
compuesta. Así `_sessions.py` no cambia de firma (sigue recibiendo un `thread_id`
opaco), y los llamantes de `register`/`resolve` en `_runner.py` y `_streaming.py`
son un cambio de una palabra cada uno. Un solo concepto —"la clave de hilo de
esta instancia"— calculado en un solo sitio.

**Por qué NO reutilizar `_options_sig()`** — incluye `system_prompt`, que los
runtimes recomponen cada turno (fecha, memoria, skills seleccionadas). Como
clave de sesión sería un fingerprint volátil: nunca matchearía y el namespacing
equivaldría a desactivar la recuperación. `_options_sig()` sigue siendo correcto
para lo suyo (reutilización de cliente en el pool), donde un system prompt
distinto SÍ invalida el cliente.

La ruta por fingerprint de prefijo (`fp:`) NO se namespacea: el digest ya cubre
el contenido completo del historial, y dos perfiles distintos sobre el mismo
historial reanudando la misma sesión es el comportamiento correcto y deseado
(es la sesión de esa conversación).

## D3. Compatibilidad

- Claves `thread:` viejas quedan huérfanas tras el upgrade. No es un problema:
  su ausencia degrada a flatten, que es exactamente el comportamiento de hoy.
  No hace falta migración ni versionado del store.
- `InMemoryStore` (default) no persiste, así que el 99% de los usuarios no ve
  ninguna diferencia hasta que opta por `session_store="file"`.
- Ningún cambio de firma pública.

## D4. Riesgo de activación

Este cambio **activa un camino que hoy está muerto**. Un consumidor con
`session_store="file"` dentro de LangGraph pasa de "siempre flatten" a
"reanuda". Es el objetivo, pero conviene enunciar lo que implica:

- La fuente de verdad del contexto se desplaza del historial del llamante a la
  sesión del CLI. Un llamante que pode su historial ya no poda el contexto real.
- Los turnos de usar-y-tirar (heartbeats, crons) NO deben reanudar. Opt-out sin
  API nueva: `session_store="memory"` es per-instancia, así que construir una
  instancia por turno nunca reanuda. Se documenta en el README.

## D5. El test que faltaba

El defecto sobrevivió porque `tests/unit_tests/test_sessions_v02.py:73` prueba
`SessionCache.resolve(msgs, thread_id="th-1")` llamando al método directamente.
La unidad estaba bien; el cableado no existía.

El test nuevo compila un `StateGraph` real, invoca el modelo desde un nodo con
`thread_id` en el config y afirma sobre la `Resolution` observada. Sin CLI: se
sondea `_resolve_session`, que es donde vive la decisión.
