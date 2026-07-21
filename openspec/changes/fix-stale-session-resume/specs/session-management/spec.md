# session-management (delta fix-stale-session-resume)

## ADDED Requirements

### Requirement: Degradación ante sesión purgada
El puente SHALL degradar de forma transparente a sesión nueva, dentro del mismo
invoke, cuando una invocación con `strategy="resume"` sobre una sesión resuelta
por el prefix-cache o por la clave de thread falla porque la sesión ya no
existe en el CLI (marcador `No conversation found with session ID`, observable
únicamente vía el callback `stderr` del SDK, que el puente SHALL registrar en
los runs que reanudan sesión). En concreto SHALL:
(1) invalidar en el store todas las entradas (`fp:` y `thread:`) que
resuelven a ese `session_id`; (2) reejecutar como sesión nueva por el camino de
degradación existente (flatten del historial completo); y (3) registrar la
sesión nueva al terminar, de modo que el turno siguiente la reanude. La
detección SHALL ocurrir antes de la contabilidad de reintentos: un resume
condenado NO SHALL consumir presupuesto de reintentos, y la reejecución como
sesión nueva SHALL disponer del presupuesto íntegro. Esto aplica tanto al
camino de invoke como al de streaming; en streaming la degradación solo SHALL
ocurrir si aún no se emitió ningún chunk. Un `session_id` fijado explícitamente
(constructor o kwarg de config) NO SHALL degradar en silencio: el error SHALL
propagarse inmediatamente, también sin consumir reintentos.

#### Scenario: Mapeo persistido hacia una sesión purgada
- **WHEN** un store persistente contiene un mapeo (por prefijo o por thread)
  hacia un `session_id` que el CLI ya purgó, y se invoca con el historial de
  esa conversación más un mensaje nuevo
- **THEN** la invocación responde con normalidad vía sesión nueva (flatten del
  historial completo), el mapeo envenenado desaparece del store y la sesión
  nueva queda registrada, de modo que la invocación siguiente la reanuda

#### Scenario: El resume condenado no consume reintentos
- **WHEN** el resume de una sesión purgada falla con el marcador y
  `max_retries` es mayor que cero
- **THEN** el resume fallido no se reintenta (exactamente una ejecución
  condenada) y la reejecución como sesión nueva conserva el presupuesto de
  reintentos íntegro

#### Scenario: Streaming sobre sesión purgada
- **WHEN** se hace `stream()` de una conversación cuyo mapeo apunta a una
  sesión purgada
- **THEN** el stream emite los chunks de la ejecución degradada como sesión
  nueva, sin propagar el error ni duplicar chunks

#### Scenario: session_id explícito sobre sesión purgada
- **WHEN** se invoca con un `session_id` fijado explícitamente (constructor o
  config) que apunta a una sesión purgada
- **THEN** el error se propaga inmediatamente (sin reintentos del resume
  condenado y sin degradación silenciosa a sesión nueva)
