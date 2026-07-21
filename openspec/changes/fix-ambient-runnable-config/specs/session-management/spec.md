# session-management (delta fix-ambient-runnable-config)

## MODIFIED Requirements

### Requirement: Prefix-cache de sesiones
El modelo SHALL mantener una caché thread-safe que mapea fingerprints de prefijos de historial a `session_id` del CLI, con **backend pluggable**: `InMemoryStore` (default, comportamiento v0.1) o `FileStore` persistente en disco (JSON con file-locking y escritura atómica, poda LRU), seleccionable vía `session_store="memory"|"file"` o instancia propia. Cuando el historial entrante es igual a un prefijo conocido más un sufijo nuevo, el modelo SHALL reanudar la sesión (`resume=session_id`) enviando únicamente el sufijo. Tras cada generación SHALL registrarse el fingerprint del historial completo resultante. El fingerprint SHALL ignorar metadata volátil y ser estable entre procesos.

El `RunnableConfig` de una invocación SHALL resolverse desde el kwarg explícito `config` si existe y, en su defecto, desde el config ambiental de langchain-core (`ensure_config()`), que es el único disponible cuando el modelo se invoca desde un nodo LangGraph. Cuando ese config aporta `configurable.thread_id`, el mapeo `thread_id → session_id` SHALL registrarse como vía de recuperación adicional cuando el prefijo no matchea.

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

### Requirement: session_id explícito
El usuario SHALL poder fijar la sesión manualmente vía `config={"configurable": {"session_id": ...}}` (prioridad sobre el prefix-cache) para reanudar sesiones existentes del CLI, replicando la capacidad de la librería antigua. Dado que `BaseChatModel.invoke/ainvoke` no propaga su parámetro `config` a `**kwargs`, esa vía SHALL resolverse por el config ambiental cuando la invocación ocurre dentro de un runnable; el atributo de constructor `session_id` SHALL seguir siendo la vía válida fuera de un runnable.

#### Scenario: Resume manual
- **WHEN** se invoca con `config={"configurable": {"session_id": "<uuid-existente>"}}`
- **THEN** la generación se ejecuta con `resume=<uuid>` sobre esa sesión

#### Scenario: Resume manual por constructor
- **WHEN** se construye el modelo con `session_id="<uuid-existente>"` y se invoca fuera de cualquier runnable
- **THEN** la generación se ejecuta con `resume=<uuid>` enviando solo el último mensaje como sufijo
