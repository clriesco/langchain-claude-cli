# session-management

## Purpose
Tender el puente entre un `BaseChatModel` sin estado y un CLI con sesiones: cuándo se reanuda una sesión, cuándo se degrada, y dónde vive ese mapeo.
## Requirements
### Requirement: Prefix-cache de sesiones
El modelo SHALL mantener una caché thread-safe que mapea fingerprints de prefijos de historial a `session_id` del CLI, con **backend pluggable**: `InMemoryStore` (default, comportamiento v0.1) o `FileStore` persistente en disco (JSON con file-locking y escritura atómica, poda LRU), seleccionable vía `session_store="memory"|"file"` o instancia propia. Cuando el historial entrante es igual a un prefijo conocido más un sufijo nuevo, el modelo SHALL reanudar la sesión (`resume=session_id`) enviando únicamente el sufijo. Tras cada generación SHALL registrarse el fingerprint del historial completo resultante. El fingerprint SHALL ignorar metadata volátil y ser estable entre procesos.

El `RunnableConfig` de una invocación SHALL resolverse desde el kwarg explícito `config` si existe y, en su defecto, desde el config ambiental de langchain-core (`ensure_config()`), que es el único disponible cuando el modelo se invoca desde un nodo LangGraph. Del config ambiental SHALL leerse **únicamente** `configurable.thread_id`: la clave `session_id` está sobrecargada en el ecosistema (`RunnableWithMessageHistory` la usa como clave de historial de chat, no como UUID de sesión del CLI) y honrarla desde el ambiente secuestraría la sesión con un valor no dirigido a este modelo. Cuando el config aporta `configurable.thread_id`, el mapeo `thread_id → session_id` SHALL registrarse como vía de recuperación adicional cuando el prefijo no matchea.

La clave de ese mapeo SHALL estar namespaced por un digest de los atributos **estables** de ejecución (`model`, `cwd`, `builtin_tools`, `permission_mode`), de modo que dos instancias del modelo con perfiles distintos que compartan `thread_id` no reanuden la sesión de la otra. El digest NO SHALL incluir `system_prompt`, que los runtimes recomponen por turno y cuya inclusión impediría toda continuidad.

#### Scenario: Conversación que crece por append
- **WHEN** se invoca con `[H1]` obteniendo `A1`, y después con `[H1, A1, H2]`
- **THEN** la segunda invocación reanuda la sesión de la primera enviando solo `H2`, y el modelo recuerda el contexto de `H1/A1` sin re-envío

#### Scenario: Instancia compartida entre conversaciones
- **WHEN** dos conversaciones distintas usan la misma instancia del modelo de forma intercalada
- **THEN** cada una reanuda su propia sesión sin cruzar contextos

#### Scenario: La conversación sobrevive a un reinicio del proceso
- **WHEN** un proceso con `session_store="file"` genera una conversación, termina, y un proceso nuevo invoca con ese historial más un mensaje nuevo
- **THEN** el proceso nuevo reanuda la sesión CLI original con fidelidad completa (sin flatten ni warning)

#### Scenario: Recuperación por thread_id
- **WHEN** un checkpointer de LangGraph recorta el historial de un `thread_id` conocido de modo que ningún prefijo matchea
- **THEN** el modelo reanuda la sesión asociada al `thread_id` en lugar de degradar a flatten, si puede determinar el sufijo nuevo

#### Scenario: thread_id ambiental dentro de un nodo LangGraph
- **WHEN** el modelo se invoca desde un nodo de un `StateGraph` compilado con `config={"configurable": {"thread_id": ...}}`, sin que el llamante pase `config` por kwarg
- **THEN** el `thread_id` se resuelve igualmente y la conversación reanuda su sesión en vez de degradar a flatten

#### Scenario: Perfiles distintos sobre el mismo thread_id
- **WHEN** dos instancias con `model` distinto (p. ej. un router barato y un ejecutor) invocan bajo el mismo `thread_id` y ninguna matchea por prefijo
- **THEN** cada una resuelve por su propia clave namespaced y ninguna reanuda la sesión de la otra

#### Scenario: El system prompt recompuesto no rompe la continuidad
- **WHEN** una conversación reanuda su sesión con un `system_prompt` distinto al del turno anterior
- **THEN** la sesión se reanuda igualmente (el perfil de la clave ignora `system_prompt`) y el system prompt nuevo se aplica al turno

### Requirement: Degradación controlada para historial arbitrario
Cuando el historial no matchea ningún prefijo cacheado ni thread conocido, el modelo SHALL usar la estrategia según `history_mode`: `"auto"`/`"flatten"` → structured flatten en un solo mensaje de usuario (multimodal preservado) con `ClaudeCliCompatWarning`; `"replay"` (EXPERIMENTAL: la fidelidad de los turnos assistant inyectados es dependiente de carrera — hallazgo de la suite de contrato) → reproducción del historial completo como mensajes user/assistant en una sesión nueva, emitiendo un warning único que documenta su coste (una generación por mensaje user histórico).

#### Scenario: Historial editado con modo auto
- **WHEN** se invoca con `history_mode="auto"` y un historial cuyo prefijo no coincide con ninguna sesión conocida
- **THEN** la invocación funciona por structured flatten y se emite el warning de degradación

#### Scenario: Replay fiel opt-in
- **WHEN** se invoca con `history_mode="replay"` y un historial arbitrario que contiene un turno assistant con un dato distintivo
- **THEN** la respuesta demuestra que el turno assistant fue honrado con fidelidad de roles (no aplanado) y se emitió el warning de coste

### Requirement: session_id explícito
El usuario SHALL poder fijar la sesión manualmente vía `config={"configurable": {"session_id": ...}}` (prioridad sobre el prefix-cache) para reanudar sesiones existentes del CLI, replicando la capacidad de la librería antigua. Esa vía SHALL resolverse únicamente desde el kwarg explícito `config` o desde el atributo de constructor `session_id` — NO SHALL resolverse desde el config ambiental, cuyo `session_id` puede pertenecer a otro componente (p. ej. `RunnableWithMessageHistory`).

#### Scenario: Resume manual
- **WHEN** se invoca con `config={"configurable": {"session_id": "<uuid-existente>"}}`
- **THEN** la generación se ejecuta con `resume=<uuid>` sobre esa sesión

#### Scenario: Resume manual por constructor
- **WHEN** se construye el modelo con `session_id="<uuid-existente>"` y se invoca fuera de cualquier runnable
- **THEN** la generación se ejecuta con `resume=<uuid>` enviando solo el último mensaje como sufijo

#### Scenario: Un session_id ambiental ajeno no secuestra la sesión
- **WHEN** el modelo se invoca desde dentro de un runnable cuyo config ambiental contiene `configurable.session_id` (p. ej. la clave de historial de `RunnableWithMessageHistory`) junto a un `thread_id`
- **THEN** ese `session_id` se ignora, la resolución no fuerza `resume` sobre él, y el `thread_id` sí se resuelve con normalidad

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

