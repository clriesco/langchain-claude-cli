# chat-model-core


### Requirement: Contrato BaseChatModel completo
`ChatClaudeCli` SHALL implementar `BaseChatModel` de langchain-core 1.x soportando `invoke`, `ainvoke`, `stream`, `astream` y `batch`/`abatch`, ejecutando cada generación vía `claude_agent_sdk.query()` contra el CLI `claude` sin requerir API key.

#### Scenario: Invoke básico
- **WHEN** se llama `ChatClaudeCli(model="claude-sonnet-4-5").invoke("Hola")`
- **THEN** devuelve un `AIMessage` con el texto de respuesta generado por el CLI

#### Scenario: Invoke asíncrono
- **WHEN** se llama `await llm.ainvoke("Hola")` dentro de un event loop
- **THEN** devuelve un `AIMessage` sin bloquear el loop ni fallar por conflicto de event loops

#### Scenario: Batch paralelo
- **WHEN** se llama `llm.batch(["a", "b", "c"])`
- **THEN** devuelve tres `AIMessage` correspondientes a cada prompt

### Requirement: Modo LLM puro por defecto
El modelo SHALL ejecutarse por defecto con `tools=[]` y `max_turns=1`, sin acceso a filesystem, shell ni red — semántica equivalente a una llamada directa a la API de Anthropic.

#### Scenario: Sin ejecución de herramientas por defecto
- **WHEN** se invoca `ChatClaudeCli()` con un prompt que pide leer un archivo local
- **THEN** el CLI no ejecuta ninguna herramienta built-in y responde solo con texto

### Requirement: Conversión fiel de mensajes LangChain
El modelo SHALL convertir mensajes LangChain a formato CLI preservando fidelidad: `SystemMessage` → system prompt, `HumanMessage` (texto e imágenes base64/URL) → content blocks, `AIMessage` con `tool_calls` → bloques `tool_use`, `ToolMessage` → bloques `tool_result`. Los bloques `cache_control` presentes en mensajes SHALL ignorarse sin error.

#### Scenario: Mensaje con imagen base64
- **WHEN** se invoca con un `HumanMessage` cuyo content incluye `{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}`
- **THEN** la imagen se envía al CLI como content block de imagen y la respuesta la describe

#### Scenario: Historial con tool calls
- **WHEN** el historial contiene `AIMessage(tool_calls=[...])` seguido de `ToolMessage`
- **THEN** la conversión no aplana los bloques a texto y el modelo responde coherentemente con el resultado de la tool

### Requirement: Metadata de uso y respuesta reales
Cada `AIMessage` generado SHALL incluir `usage_metadata` (input_tokens, output_tokens, y `input_token_details` con cache_read/cache_creation cuando el CLI los reporte) y `response_metadata` con al menos `stop_reason`, `model`, `session_id` y `total_cost_usd`.

#### Scenario: Usage tras invoke
- **WHEN** se completa un `invoke`
- **THEN** `result.usage_metadata["input_tokens"]` y `["output_tokens"]` son enteros > 0 y `response_metadata["session_id"]` es un UUID válido

### Requirement: Compatibilidad de firma con ChatAnthropic
El constructor SHALL aceptar todos los parámetros del constructor de `ChatAnthropic` 1.4.x sin lanzar excepción. Los parámetros sin soporte CLI (`temperature`, `top_k`, `top_p`, `anthropic_api_url`, `anthropic_proxy`, `default_headers`, `inference_geo`, `anthropic_api_key`) SHALL aceptarse como no-op emitiendo un `ClaudeCliCompatWarning` una sola vez por proceso y por parámetro.

#### Scenario: Constructor con parámetros no soportados
- **WHEN** se construye `ChatClaudeCli(model="...", temperature=0.2, top_k=40)`
- **THEN** la construcción no falla, se emite un warning por cada parámetro ignorado, y el invoke funciona

#### Scenario: Warning único
- **WHEN** se construyen dos instancias con `temperature=0.5`
- **THEN** el warning de `temperature` se emite solo la primera vez

### Requirement: Parámetros con workaround
`max_tokens` SHALL aplicarse por truncado del lado cliente (marcando `stop_reason="max_tokens"` sintético); `stop_sequences` SHALL aplicarse deteniendo y truncando la salida en la primera ocurrencia; `max_retries` SHALL reintentar ante errores API transitorios (429/5xx vía `api_error_status`); `timeout` SHALL cancelar la generación al excederse. `get_num_tokens_from_messages` SHALL devolver una estimación heurística documentada como aproximada.

#### Scenario: Stop sequence
- **WHEN** se invoca con `stop=["FIN"]` y el modelo genera texto que contiene "FIN"
- **THEN** el contenido devuelto termina justo antes de "FIN" y `stop_reason` es `"stop_sequence"`

#### Scenario: Reintento ante error transitorio
- **WHEN** una generación falla con `api_error_status=529` y `max_retries>=1`
- **THEN** el modelo reintenta automáticamente antes de propagar el error

### Requirement: Extended thinking y effort nativos
El parámetro `thinking={"type": "enabled", "budget_tokens": N}` SHALL pasarse nativamente al SDK (mismo formato) y los bloques de razonamiento SHALL aparecer como content blocks de tipo `thinking` en el `AIMessage`. El parámetro `effort` SHALL mapearse al campo `effort` del SDK.

#### Scenario: Thinking habilitado
- **WHEN** se invoca con `thinking={"type": "enabled", "budget_tokens": 5000}` y un problema de razonamiento
- **THEN** el `AIMessage` contiene al menos un content block `thinking` además del texto final

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

### Requirement: Watchdog de inactividad del stream
El modelo SHALL aceptar `inactivity_timeout: float | None`: si el stream del SDK no produce ningún mensaje durante ese intervalo, el run SHALL abortarse cerrando el stream (sin dejar subprocesos huérfanos) y lanzando `ClaudeCliError` con el intervalo en el mensaje. Defaults: 120s en modo LLM puro; desactivado (`None`) en modo agéntico (las tools legítimas producen silencios largos); siempre configurable. `default_request_timeout` sigue actuando como techo total independiente.

#### Scenario: CLI muerto a mitad de run
- **WHEN** el subproceso CLI muere sin cerrar el stream y hay `inactivity_timeout` activo
- **THEN** el invoke lanza `ClaudeCliError` al expirar el intervalo, sin quedar colgado y sin dejar proceso huérfano

#### Scenario: Turno agéntico largo no afectado
- **WHEN** un run agéntico con tools lentas guarda silencio más de 120s con el default agéntico
- **THEN** el run NO se aborta (watchdog desactivado por defecto en agéntico)

### Requirement: Logging estructurado
La librería SHALL emitir eventos por el logger `langchain_claude_cli` (con `NullHandler` por defecto): DEBUG para resolución de sesión (estrategia, prefijo/thread), pool (hit/miss/evicción) y defer/delivery; INFO/WARNING para reintentos, watchdog y degradaciones de historial. Sin handler configurado, la librería SHALL permanecer silenciosa.

#### Scenario: Diagnóstico activable
- **WHEN** el usuario configura `logging.getLogger("langchain_claude_cli").setLevel(DEBUG)` con un handler
- **THEN** un invoke multi-turn muestra la resolución de sesión (resume + longitud del sufijo) y los reintentos si los hay
