# Design — fix-stale-session-resume

## D1. Dónde es observable el marcador

Medición contra el CLI real (resume de un UUID inexistente):

- El CLI imprime `No conversation found with session ID: <uuid>` en **stderr**
  y sale con exit 1.
- El transport del SDK (`subprocess_cli.py`) construye
  `ProcessError("Command failed with exit code 1", exit_code=1,
  stderr="Check stderr output for details")` — el atributo `stderr` es ese
  **literal**, no el stderr real. `str(exc)` tampoco contiene el marcador.
- El texto real solo llega registrando el callback `options.stderr` del SDK
  (`Callable[[str], None]`), que fuerza el pipe del stderr del subproceso.

Regla adoptada: los runs cuya `Resolution` es `strategy="resume"` registran un
colector — un `deque(maxlen=50)` de líneas, reemitidas a nivel DEBUG por el
logger `langchain_claude_cli` para no perder observabilidad. El detector
`_is_stale_session_error(exc, stderr_lines)` comprueba el marcador en
`str(exc)`, en `exc.stderr` y en el búfer (los dos primeros por robustez ante
futuras versiones del SDK que sí lo propaguen; el tercero es el que hoy
funciona).

Coste/efecto lateral: en esos runs el stderr del CLI deja de heredarse al
terminal (queda en el log DEBUG). Solo afecta a runs con resume; el resto no
registra callback y no cambia.

## D2. Dónde se decide la degradación

La detección vive en el manejador de excepciones del bucle de ejecución,
ANTES de la contabilidad de reintentos — el mismo lugar y espíritu que
`_is_contradictory_success` (0.4.1): clasificar el fallo primero, reintentar
después. Un resume de sesión purgada falla siempre; reintentarlo solo quema
presupuesto y tiempo (backoff exponencial) para llegar al mismo sitio.

`_arun_query_inner` pasa a tener un bucle exterior de **rondas de sesión**
(máximo 2):

```
ronda 1: resolution = _resolve_session(...)   # resume <sid-purgado>
         └─ falla con marcador → invalidate(sid); resolution = new; continue
ronda 2: resolution = new (flatten historial completo)
         └─ bucle de reintentos ÍNTEGRO para la ejecución real
```

Todo lo que depende de la `Resolution` (converted, pending_ids, delivery,
entries, system+tool_choice, options) se reconstruye dentro de la ronda: el
camino "new" debe ser idéntico al que habría producido `_resolve_session` sin
entrada en cache (mismos flatten, warnings y semántica de delivery). La ronda
2 no puede volver a degradar (una sesión nueva no lleva `resume`), así que el
bucle termina siempre.

Al terminar la ronda 2 con éxito, el registro de la sesión nueva es el de
siempre (`_build_chat_result` / cierre de `_astream`): el fingerprint del
historial completo y la clave `thread:` pasan a apuntar al `session_id` nuevo,
y el turno siguiente reanuda esa sesión. No hace falta código nuevo para el
punto 3 del comportamiento requerido.

## D3. `session_id` explícito: propagar, pero rápido

Cuando la sesión la fijó el llamante (constructor
`ChatClaudeCli(session_id=...)` o el kwarg interno de config), degradar en
silencio sería sustituir la sesión pedida por una vacía: el llamante cree que
conserva el contexto de esa sesión y no es así — corrupción silenciosa de
conversación. Se propaga el error original (`ProcessError`).

Matiz deliberado: el marcador se detecta **también** en este camino, para
propagar inmediatamente sin consumir reintentos (el resume condenado no mejora
reintentándolo). `Resolution` gana un campo interno `explicit: bool = False`
que `_resolve_session` activa en el camino explícito; la degradación solo
aplica a `resume and not explicit`.

Alternativa considerada — degradar también el explícito con un warning: se
descarta porque el contrato del parámetro es "reanuda exactamente esta
sesión"; quien quiera tolerancia a purga tiene el prefix-cache, que es
justamente la vía que degrada con seguridad (el historial completo viaja en
cada invoke y no se pierde nada).

## D4. Invalidación en el store

`SessionCache.invalidate(session_id)` recorre las claves del store y elimina
toda entrada (`fp:` y `thread:`) cuyo `session_id` coincida, purgándolas
también de la lista LRU (`__order__`). Se invalida por `session_id` y no solo
por la clave que resolvió: un mismo `session_id` purgado puede estar colgado de
varios fingerprints (prefijos crecientes de la misma conversación) y de la
clave de thread; dejar hermanos envenenados reproduciría el bug en el
siguiente resolve. Thread-safe con el lock existente; devuelve el número de
entradas eliminadas (útil para logging y tests).

## D5. Streaming y pool

- `_astream` duplica el flujo de resolución/ejecución y recibe el mismo bucle
  de rondas. Salvaguarda adicional: solo se degrada si aún no se ha emitido
  ningún chunk al consumidor (el fallo de sesión purgada ocurre al arrancar el
  proceso, antes de cualquier token — la guarda es por si un fallo tardío
  contuviera el marcador; reemitir chunks duplicados sería peor que propagar).
- `_pool.run_turn` ya captura cualquier excepción del cliente vivo, hace evict
  y devuelve `None`, con lo que el llamante cae al camino stateless — donde
  aplica la detección nueva. Un cliente pooled sobre sesión purgada termina,
  por tanto, degradando igual. Sin cambios en `_pool.py`.

## D6. Tests

Sin CLI: dobles de `claude_agent_sdk.query` (patrón de `test_watchdog.py`) que
(a) invocan el callback `options.stderr` con la línea del marcador y lanzan un
`ProcessError` real del SDK, y (b) responden con `AssistantMessage`/
`ResultMessage` reales en la ronda degradada. Se afirma: respuesta correcta,
solo 2 llamadas a query con `max_retries` alto (presupuesto no consumido),
`resume=None` en la segunda, store invalidado y sesión nueva registrada,
propagación inmediata con `session_id` explícito, y el mismo flujo vía
`stream()`. `invalidate()` tiene test unitario propio. La verificación e2e
contra el CLI real (mapeo envenenado desde un nodo LangGraph) queda como
comprobación manual final documentada en tasks.md.
