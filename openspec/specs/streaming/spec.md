# streaming

## Purpose
Definir la traducción de los eventos crudos del SDK a `AIMessageChunk`, para que el streaming sea token a token real y no un troceado a posteriori.

## Requirements

### Requirement: Streaming token a token real
`stream` y `astream` SHALL activar `include_partial_messages=True` y traducir los `StreamEvent` del SDK (eventos raw de la API Anthropic) a `AIMessageChunk`: deltas de texto como contenido incremental, deltas de thinking como content blocks `thinking`, y `content_block_start`/`input_json_delta` de tool_use como `tool_call_chunks`. El streaming sync SHALL funcionar fuera y dentro de un event loop existente (patrón thread+queue).

#### Scenario: Stream de texto
- **WHEN** se itera `llm.stream("Cuenta del 1 al 5")`
- **THEN** se reciben múltiples chunks incrementales (no uno solo) cuyo contenido concatenado forma la respuesta completa

#### Scenario: Stream con thinking
- **WHEN** se hace stream con `thinking` habilitado
- **THEN** los chunks de razonamiento llegan como bloques `thinking` diferenciados de los de texto final

#### Scenario: Stream de tool calls
- **WHEN** se hace stream con tools bound y el modelo llama una tool
- **THEN** se reciben `tool_call_chunks` incrementales y el mensaje agregado final tiene `tool_calls` completos

### Requirement: Usage en el chunk final
El último chunk del stream SHALL incluir `usage_metadata` y `response_metadata` (stop_reason, session_id, total_cost_usd), replicando el comportamiento `stream_usage=True` de ChatAnthropic.

#### Scenario: Usage al final del stream
- **WHEN** se consume un stream completo y se agregan los chunks
- **THEN** el mensaje agregado tiene `usage_metadata` con tokens > 0

### Requirement: Actividad de tools built-in en el stream (modo agéntico)
Cuando el CLI ejecuta tools built-in o MCP dentro del run (modo agéntico), cada tool call completada SHALL emitirse como un chunk cuyo content contiene un bloque `tool_use` con `id`, `name` e `input` completos (buffer de `input_json_delta` hasta `content_block_stop`), con índice sintético monotónico para evitar colisiones de merge entre mensajes assistant del mismo run.

#### Scenario: Stream agéntico con Read
- **WHEN** un modelo con `builtin_tools=["Read"]` hace stream de una tarea que lee un archivo
- **THEN** el stream contiene un chunk con bloque `tool_use` de nombre `Read` además de los chunks de texto de la respuesta

### Requirement: stop_sequences en streaming
Con `stop_sequences` configuradas, el stream SHALL detenerse al detectar la secuencia: el chunk que la contiene se trunca antes de la secuencia, no se emiten más chunks y la generación subyacente se cancela.

#### Scenario: Corte por stop sequence en stream
- **WHEN** se hace stream con `stop=["###"]` y el modelo genera "hola###adios"
- **THEN** el contenido emitido total es "hola" y el stream termina inmediatamente
