# session-management (delta v0.2)

## MODIFIED Requirements

### Requirement: Prefix-cache de sesiones
El modelo SHALL mantener una caché thread-safe que mapea fingerprints de prefijos de historial a `session_id` del CLI, con **backend pluggable**: `InMemoryStore` (default, comportamiento v0.1) o `FileStore` persistente en disco (JSON con file-locking y escritura atómica, poda LRU), seleccionable vía `session_store="memory"|"file"` o instancia propia. Cuando el historial entrante es igual a un prefijo conocido más un sufijo nuevo, el modelo SHALL reanudar la sesión (`resume=session_id`) enviando únicamente el sufijo. Tras cada generación SHALL registrarse el fingerprint del historial completo resultante. El fingerprint SHALL ignorar metadata volátil y ser estable entre procesos. Cuando el invoke incluye `config.configurable.thread_id`, el mapeo `thread_id → session_id` SHALL registrarse como vía de recuperación adicional cuando el prefijo no matchea.

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

### Requirement: Degradación controlada para historial arbitrario
Cuando el historial no matchea ningún prefijo cacheado ni thread conocido, el modelo SHALL usar la estrategia según `history_mode`: `"auto"`/`"flatten"` → structured flatten en un solo mensaje de usuario (multimodal preservado) con `ClaudeCliCompatWarning`; `"replay"` → reproducción fiel del historial completo como mensajes user/assistant en una sesión nueva, emitiendo un warning único que documenta su coste (una generación por mensaje user histórico).

#### Scenario: Historial editado con modo auto
- **WHEN** se invoca con `history_mode="auto"` y un historial cuyo prefijo no coincide con ninguna sesión conocida
- **THEN** la invocación funciona por structured flatten y se emite el warning de degradación

#### Scenario: Replay fiel opt-in
- **WHEN** se invoca con `history_mode="replay"` y un historial arbitrario que contiene un turno assistant con un dato distintivo
- **THEN** la respuesta demuestra que el turno assistant fue honrado con fidelidad de roles (no aplanado) y se emitió el warning de coste
