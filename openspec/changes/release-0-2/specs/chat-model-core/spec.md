# chat-model-core (delta v0.2)

## ADDED Requirements

### Requirement: Estado de rate limits en response_metadata
Cuando el SDK emite `RateLimitEvent` durante un run, el modelo SHALL exponer el último estado recibido en `response_metadata["rate_limit"]` con las claves `status`, `type`, `utilization` y `resets_at`. Si el CLI no emite el evento (p.ej. modo no-interactivo sin soporte), la clave SHALL estar ausente sin producir error.

#### Scenario: Rate limit presente
- **WHEN** un run recibe un `RateLimitEvent` del SDK
- **THEN** la respuesta incluye `response_metadata["rate_limit"]["status"]` con el valor reportado

#### Scenario: Rate limit ausente
- **WHEN** un run no recibe ningún `RateLimitEvent`
- **THEN** `response_metadata` no contiene la clave `rate_limit` y la generación funciona con normalidad

### Requirement: Bloques Files API
La conversión de mensajes SHALL aceptar bloques de documento/imagen con `source: {"type": "file", "file_id": ...}` (formato Files API de ChatAnthropic). Si el CLI los acepta nativamente (según spike S7), SHALL hacerse passthrough; si no, y hay `ANTHROPIC_API_KEY` disponible, el contenido SHALL materializarse vía API antes del envío; sin ninguna de las dos vías, el bloque SHALL omitirse emitiendo `ClaudeCliCompatWarning` (nunca romper la invocación).

#### Scenario: file_id sin soporte ni API key
- **WHEN** se invoca con un bloque `file_id` en un entorno sin soporte CLI ni API key
- **THEN** la invocación completa con el resto del contenido y se emite un warning indicando el bloque omitido
