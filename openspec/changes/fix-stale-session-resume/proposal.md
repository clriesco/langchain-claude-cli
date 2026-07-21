# fix-stale-session-resume

## Why

El `SessionCache` mapea claves de hilo y fingerprints de prefijo →
`session_id` del CLI, persistidas en disco con `session_store="file"`. Pero el
CLI purga transcripts inactivos (`cleanupPeriodDays`, ~30 días por defecto),
así que un mapeo persistido puede apuntar a una sesión que **ya no existe**.

Hoy, al reanudar una sesión purgada (verificado empíricamente contra el CLI):

1. El CLI imprime `No conversation found with session ID: <uuid>` en stderr y
   sale con exit 1.
2. El SDK lo convierte en `ProcessError: Command failed with exit code 1` —
   que el bucle de reintentos trata como error de transporte y **reintenta el
   mismo resume**, condenado a fallar siempre.
3. Agotados los reintentos, el invoke revienta con `ProcessError`.
4. El mapeo envenenado **queda en el store**: todos los turnos siguientes de
   esa conversación fallan igual. Rotura permanente hasta borrar el fichero a
   mano.

Es decir: la persistencia que en 0.4.2 pasó a funcionar de verdad (recuperación
por `thread_id` ambiental) convierte una limpieza rutinaria del CLI en una
avería permanente del consumidor.

Un hallazgo empírico condiciona el diseño: el marcador `No conversation found
with session ID` **no viaja en la excepción**. El transport del SDK construye
el `ProcessError` con el literal `"Check stderr output for details"` como
`stderr`; el texto real solo es observable registrando el callback
`options.stderr` del SDK (medido contra el CLI real). Ver design D1.

## What Changes

- **Degradación transparente a sesión nueva**: cuando un run con
  `strategy="resume"` sobre una sesión resuelta por el cache falla con el
  marcador de sesión inexistente, el puente — en el mismo invoke — invalida
  las entradas del store que resolvieron a ese `session_id`, reejecuta como
  sesión nueva (flatten del historial completo, el camino que ya existe) y
  registra la sesión nueva al terminar, de modo que el turno siguiente la
  reanuda con normalidad.
- **Detección antes de reintentar**: el marcador se comprueba en el manejador
  de errores ANTES de la contabilidad de reintentos (mismo espíritu que
  `_is_contradictory_success` en 0.4.1). Un resume condenado no consume
  presupuesto de reintentos; la reejecución como sesión nueva dispone del
  presupuesto íntegro.
- **Captura de stderr acotada**: los runs que reanudan sesión registran un
  colector `options.stderr` (búfer acotado) para poder observar el marcador.
  Es la única vía donde el texto es visible (ver design D1).
- **`SessionCache.invalidate(session_id)`**: nuevo método interno que elimina
  del store todas las entradas (`fp:` y `thread:`) que resuelven a ese
  `session_id` y las purga de la lista LRU.
- **`session_id` explícito NO degrada en silencio**: si la sesión la fijó el
  llamante (constructor `ChatClaudeCli(session_id=...)` o
  `config={"configurable": {"session_id": ...}}` por kwarg interno), el
  llamante pidió *esa* sesión concreta; sustituirla por una nueva vacía sería
  perder contexto sin avisar. El error se propaga — pero se detecta igualmente
  el marcador para **fallar rápido** sin quemar reintentos condenados
  (decisión razonada en design D3).
- Cubre **ambos** caminos: invoke (`_runner.py`) y streaming
  (`_streaming.py`). El camino persistente (`_pool.py`) ya cae de vuelta al
  camino stateless ante cualquier excepción del cliente vivo, donde aplica la
  detección nueva — no requiere cambios (design D5).
- Sin API nueva pública: `invalidate` es interno, la degradación es
  transparente y no hay cambios de firma.

## Impact

- Specs: `session-management` (ADDED: requirement de degradación ante sesión
  purgada)
- Código: `_runner.py`, `_streaming.py`, `_sessions.py`
- Compatibilidad: ningún cambio de firma pública. El único comportamiento que
  cambia es el camino que hoy termina en `ProcessError` irrecuperable: pasa a
  responder. Los runs que no reanudan sesión no registran colector de stderr y
  no cambian en nada.
- Efecto lateral menor y deliberado: en los runs que reanudan sesión resuelta
  por cache, el stderr del CLI pasa de heredarse al terminal a capturarse en el
  colector (se reemite vía logging a nivel DEBUG). Ver design D1.
