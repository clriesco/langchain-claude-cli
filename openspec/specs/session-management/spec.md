# session-management


### Requirement: Prefix-cache de sesiones
El modelo SHALL mantener una caché LRU thread-safe que mapea fingerprints de prefijos de historial a `session_id` del CLI. Cuando el historial entrante es igual a un prefijo conocido más un sufijo nuevo, el modelo SHALL reanudar la sesión (`resume=session_id`) enviando únicamente el sufijo. Tras cada generación SHALL registrarse el fingerprint del historial completo resultante. El fingerprint SHALL ignorar metadata volátil (ids de run, response_metadata) y basarse en roles y contenido normalizado.

#### Scenario: Conversación que crece por append
- **WHEN** se invoca con `[H1]` obteniendo `A1`, y después con `[H1, A1, H2]`
- **THEN** la segunda invocación reanuda la sesión de la primera enviando solo `H2`, y el modelo recuerda el contexto de `H1/A1` sin re-envío

#### Scenario: Instancia compartida entre conversaciones
- **WHEN** dos conversaciones distintas usan la misma instancia del modelo de forma intercalada
- **THEN** cada una reanuda su propia sesión sin cruzar contextos

### Requirement: Degradación controlada para historial arbitrario
Cuando el historial no matchea ningún prefijo cacheado, el modelo SHALL usar la mejor estrategia disponible en este orden: (1) replay completo del historial en una sesión nueva si el CLI acepta mensajes assistant en el input (según spike S3); (2) aplanado a texto como último recurso, emitiendo `ClaudeCliCompatWarning` de degradación de fidelidad.

#### Scenario: Historial editado
- **WHEN** se invoca con un historial cuyo prefijo no coincide con ninguna sesión conocida (p.ej. mensajes recortados por trimming)
- **THEN** la invocación funciona por replay o flatten, y si es flatten se emite el warning de degradación

### Requirement: session_id explícito
El usuario SHALL poder fijar la sesión manualmente vía `config={"configurable": {"session_id": ...}}` (prioridad sobre el prefix-cache) para reanudar sesiones existentes del CLI, replicando la capacidad de la librería antigua.

#### Scenario: Resume manual
- **WHEN** se invoca con `config={"configurable": {"session_id": "<uuid-existente>"}}`
- **THEN** la generación se ejecuta con `resume=<uuid>` sobre esa sesión
