# Design: create-langchain-claude-cli

## Context

Greenfield. El repo contiene únicamente el clon de referencia `langchain-claude-code/` (lib vieja sobre `claude-code-sdk` 0.0.20) y este OpenSpec. Toda la investigación se hizo contra el código real de los dos paquetes que definen el problema:

- **Contrato a replicar**: `langchain-anthropic` 1.4.8 — `ChatAnthropic` con `bind_tools(tools, tool_choice, parallel_tool_calls, strict)`, `with_structured_output(schema, include_raw, method)`, `get_num_tokens_from_messages`, thinking, effort, server tools, cache_control, citations, y un módulo `middleware/` para `create_agent`.
- **Motor**: `claude-agent-sdk` 0.2.115 — verificado del wheel de PyPI:
  - `query()` (async generator) y `ClaudeSDKClient` (sesión persistente, `interrupt()`, `set_model()`).
  - `ClaudeAgentOptions`: `tools` (lista o preset; `[]` = sin tools built-in), `allowed_tools`/`disallowed_tools`, `system_prompt` (str | preset claude_code | file), `mcp_servers` (stdio/sse/http/**sdk in-process**), `permission_mode`, `resume`/`fork_session`/`continue_conversation`/`session_id`, `session_store` (protocolo pluggable), `max_turns`, `max_budget_usd`, `model`/`fallback_model`, `thinking` (`{"type":"enabled","budget_tokens":N}` — idéntico a ChatAnthropic), `effort`, `output_format`, `max_thinking_tokens`, `include_partial_messages`, `hooks`, `can_use_tool`, `cwd`, `env`, `extra_args`, `betas`, `sandbox`, `agents`, `load_timeout_ms`.
  - Hook `PreToolUse` puede devolver `permissionDecision: "defer"` → el run se detiene y `ResultMessage.deferred_tool_use` (`{id, name, input}`, **singular**) llega al caller.
  - `ResultMessage`: `stop_reason`, `usage`, `model_usage`, `total_cost_usd`, `structured_output`, `permission_denials`, `api_error_status`, `session_id`.
  - `StreamEvent.event` = evento raw de la API Anthropic (deltas de texto/thinking/tool-input).
  - `@tool` + `create_sdk_mcp_server` → tools in-process con naming `mcp__<server>__<tool>`.

Restricciones: requiere CLI `claude` autenticado (Pro/Max); un subproceso por query (latencia ~1-3s de arranque); el CLI no expone sampling params (temperature/top_k/top_p) ni max_tokens ni stop_sequences ni tool_choice forzado; sin API key no hay endpoint `count_tokens`.

## Goals / Non-Goals

**Goals:**

- Drop-in real: `from langchain_claude_cli import ChatClaudeCli` sustituye a `ChatAnthropic` sin romper firmas — lo no soportado se acepta y degrada con warning, nunca con excepción de constructor.
- Patrón de tool calling LangChain clásico: el modelo devuelve `AIMessage.tool_calls`, el grafo ejecuta, el ciclo se cierra con fidelidad (sin aplanar historial).
- Modo por defecto = LLM puro (`tools=[]`, sin filesystem). Agéntico opt-in.
- Fidelidad de mensajes: multimodal, tool blocks, thinking blocks, usage_metadata con cache tokens.
- Python ≥3.10, `langchain-core` 1.x.

**Non-Goals (v0.1):**

- Réplica del módulo `middleware/` de langchain-anthropic (el CLI trae bash/editor/memoria nativos).
- Computer use, citations con spans reales, logprobs.
- `ClaudeSDKClient` persistente entre invokes (se usa `query()` stateless + resume; el cliente persistente queda para v0.2).
- Emulación de sampling params (imposible vía CLI).
- Soporte TypeScript.

## Decisions

### D1 — Nombre y layout: paquete `langchain-claude-cli`, módulo `langchain_claude_cli`, flat en el raíz del repo

Repo mono-propósito → `pyproject.toml` en el raíz con `langchain_claude_cli/` como paquete (no `libs/` intermedio: solo hay una lib y simplifica tooling). El clon `langchain-claude-code/` se mantiene como referencia hasta el final y se elimina antes del primer release. Clase principal: `ChatClaudeCli`.

*Alternativa descartada*: layout `libs/claude-cli/` estilo monorepo langchain — sobreingeniería para una sola lib.

### D2 — Motor de ejecución: `query()` por invocación + resume de sesión, no cliente persistente

Cada `_generate`/`_stream` lanza `query()` con opciones construidas ad hoc. El multi-turn se logra con `resume=<session_id>` (ver D4), no manteniendo un `ClaudeSDKClient` vivo. Razón: `BaseChatModel` es stateless y sus instancias se comparten entre cadenas/hilos; un cliente persistente introduce ciclo de vida (¿quién lo cierra?) y estado compartido mutable. El coste es re-arrancar el subproceso por invoke, mitigado por resume (el CLI recarga la sesión de disco).

*Alternativa descartada*: pool de `ClaudeSDKClient` — complejidad de lifecycle sin beneficio claro en v0.1; reevaluar en v0.2 para interrupts y `set_model`.

### D3 — Tool calling clásico: MCP in-process + hook defer

`bind_tools(tools)`:

1. Convierte cada tool (BaseTool | dict | Pydantic | callable) a schema vía `convert_to_openai_tool` de langchain-core (mismo camino que ChatAnthropic).
2. Registra un servidor MCP in-process `create_sdk_mcp_server(name="lc")` cuyos handlers **nunca se ejecutan** en modo clásico.
3. Añade hook `PreToolUse` con matcher `mcp__lc__.*` que devuelve `{"permissionDecision": "defer"}`.
4. El run se detiene con `stop_reason="tool_deferred"`. `AIMessage.tool_calls` se construye desde **todos los `ToolUseBlock`** de los `AssistantMessage` del stream (verificado S2: el campo `deferred_tool_use` es singular y solo lleva la última — no se usa como fuente). El texto emitido después del primer tool_use diferido se descarta (es la reacción del modelo al defer). Nombres des-namespaceados (`mcp__lc__x` → `x`).
5. El invoke siguiente llega con `[..., AIMessage(tool_calls), ToolMessage(s)]`; el prefix-cache (D4) resuelve la sesión y se reanuda con **prompt vacío**: el CLI re-dispara automáticamente las tool calls pendientes contra el servidor MCP, cuyos handlers entregan el contenido de los `ToolMessage` (mapa `tool name/id → resultado` poblado desde el sufijo). En la pata de resume no se instala el hook defer para las calls ya resueltas. Los handlers deben ser **idempotentes** (verificada doble invocación en S1b). *(Ajustado tras spike S1: enviar `tool_result` como mensaje de usuario NO funciona — el CLI lo trata como mensaje vacío.)*

`tool_choice`: `"auto"`/None nativo; `"any"` y tool específica → instrucción de sistema + validación del resultado con un reintento; si sigue sin llamar tool, error explícito (nivel B documentado). `parallel_tool_calls`: **soportado nativamente** (verificado S2: N tool calls se difieren todas y el resume las re-dispara todas). `strict`: no soportado, warning.

*Alternativa descartada*: inyección de schemas en prompt + parseo JSON (lib vieja) — frágil, sin garantía de schema; queda solo como **fallback** si el spike S1 revela bloqueos del mecanismo defer.

### D4 — Historial multi-turn: session prefix-cache con degradación controlada

Problema: `BaseChatModel` recibe el historial completo en cada invoke; el CLI es una sesión con estado.

Estrategia en cascada:

1. **Prefix-cache (camino fiel)**: caché LRU en memoria (compartida por instancia, thread-safe) que mapea `fingerprint(mensajes[0..k])` → `session_id`. Si el historial entrante = prefijo conocido + sufijo nuevo → `resume=session_id` y se envía solo el sufijo (incl. tool_results). Tras cada respuesta se registra el nuevo fingerprint. Cubre el 95% de usos reales: conversación que crece por append (chatbots, agentes LangGraph, ciclo de tool calling).
2. **Structured flatten** (fallback por defecto para historial arbitrario): todo el historial en UN solo mensaje de usuario cuyos content blocks preservan imágenes/documents intactos y etiquetan los roles en bloques de texto → una sola generación, multimodal sin pérdida. Emite warning de degradación de fidelidad de roles. *(Ajustado tras spike S3: el replay multi-mensaje funciona y los turnos assistant fabricados se honran, pero cada mensaje user histórico dispara una generación en vivo — coste O(turnos) inaceptable como default; queda disponible como estrategia opt-in `history_mode="replay"`.)*

El fingerprint hashea contenido normalizado de mensajes (roles + bloques), ignorando metadata volátil (ids de run, response_metadata).

*Alternativa descartada*: sintetizar transcript JSONL + `session_store`/materialización — usa formato interno no estable del CLI; se anota como spike futuro, no v0.1.

### D5 — Structured output: `output_format` nativo con dos métodos

- `method="json_schema"` (default preferente): `options.output_format = {schema JSON}` → `ResultMessage.structured_output` ya parseado. Pydantic valida si el schema era una clase Pydantic.
- `method="function_calling"`: compat de firma — se implementa sobre el mismo defer de D3 (la tool es el schema) para quien dependa de ese método.
- `include_raw=True`: devuelve `{"raw": AIMessage, "parsed": obj, "parsing_error": exc|None}` — idéntico a ChatAnthropic.

### D6 — Matriz de compat de parámetros (política de degradación)

Regla única: **el constructor nunca rompe por un parámetro que ChatAnthropic acepta**.

- **Nativos**: `model`, `thinking` (dict passthrough — formato idéntico), `effort` (passthrough — los 5 niveles `max/xhigh/high/medium/low` coinciden exactamente, verificado S1), `betas` (los que existan en `SdkBeta`), `mcp_servers` (traducir formato API → formato CLI), `max_retries` (loop propio sobre `api_error_status` 429/5xx), `timeout` (cancelación asyncio + `load_timeout_ms`).
- **Workaround**: `stop_sequences` (escaneo del stream, truncar + `interrupt()`/cortar el generator), `max_tokens` (truncado de salida con `finish_reason="length"` sintético), `get_num_tokens_from_messages` (heurística chars/token documentada como estimación).
- **No-op + warning único por proceso** (`warnings.warn`, categoría propia `ClaudeCliCompatWarning`): `temperature`, `top_k`, `top_p`, `anthropic_api_url`, `anthropic_proxy`, `default_headers`, `inference_geo`, `stream_usage=False`, `cache_control` en mensajes (se ignora en conversión), `citations` en respuesta.

### D7 — Streaming: `include_partial_messages` + traducción de eventos raw

`_stream`/`_astream` activan `include_partial_messages=True` y traducen `StreamEvent.event` (formato API Anthropic) a `AIMessageChunk`:

- `content_block_delta.text_delta` → chunk de texto.
- `content_block_delta.thinking_delta` → chunk con bloque `thinking` (content blocks v1 de langchain-core).
- `content_block_delta.input_json_delta` + `content_block_start.tool_use` (tools LangChain `mcp__lc__*`) → `tool_call_chunks`.
- Tools built-in/MCP ejecutadas in-run (modo agéntico): se bufferiza su `input_json_delta` y en `content_block_stop` se emite un chunk con bloque `tool_use` completo (índice sintético monotónico — los índices de la API se reinician por mensaje assistant y colisionarían en el merge).
- `ResultMessage` final → chunk con `usage_metadata` + `response_metadata` (paridad con `stream_usage=True` de ChatAnthropic).

Sync `_stream` reutiliza el patrón thread+queue de la lib vieja (probado); async es passthrough natural.

### D8 — Conversión de mensajes: módulo propio `_convert.py` con fidelidad de bloques

LangChain → CLI (stream-json input): system → `options.system_prompt`; human (texto/imagen/document) → user message con content blocks; AIMessage con tool_calls → assistant blocks (solo para replay S3); ToolMessage → user message con `tool_result` block. CLI → LangChain: `TextBlock`/`ThinkingBlock`/`ToolUseBlock` → content blocks v1 + `tool_calls`; `usage`/`model_usage` → `usage_metadata` (incl. `input_token_details.cache_read/cache_creation`); `stop_reason`, `session_id`, `total_cost_usd`, `model` → `response_metadata`.

### D9 — Modo agéntico (paridad con lib vieja) como capa opt-in

Por defecto `tools=[]` y `max_turns=1`. Parámetros opt-in: `builtin_tools` (lista o preset `"claude_code"`), `allowed_tools`/`disallowed_tools`, `permission_mode`, `cwd`, `max_turns`, `max_budget_usd`, `sandbox`. Se re-exporta un enum `ClaudeTool` + presets (`READ_ONLY_TOOLS`, etc.) equivalentes a los de la lib vieja. En modo agéntico las tools built-in se ejecutan de verdad (no defer); el hook defer solo matchea `mcp__lc__.*`, así conviven tools LangChain diferidas con tools CLI ejecutadas.

### D10 — Spikes primero, con criterios de salida

Los 5 spikes del proposal se implementan como scripts ejecutables en `spikes/` antes del grueso del código; sus resultados fijan qué rama de D3/D4 se activa. Cada spike deja conclusión escrita en `spikes/FINDINGS.md`. S1 (defer round-trip) es bloqueante: si falla, D3 cae al fallback de prompt-injection y se replantea.

## Risks / Trade-offs

- **[Defer es singular]** Si Claude emite varias tool calls en un turno quizá solo se difiere la primera → paralelismo degradado a secuencial. Mitigación: spike S2; documentar; `parallel_tool_calls=False` implícito si se confirma.
- **[Formato interno del CLI cambia rápido]** ~90 releases en un año; `deferred_tool_use`, `stop_reason` y `output_format` son recientes. Mitigación: pin `claude-agent-sdk>=0.2.115,<0.3`; CI de integración contra latest; los spikes se convierten en tests de contrato.
- **[Prefix-cache falla silenciosamente]** Historiales editados (trimming, summarization de LangGraph) rompen el prefijo → degradación a replay/flatten. Mitigación: warning en degradación + doc de la matriz de fidelidad; el fingerprint ignora metadata volátil para minimizar misses.
- **[Latencia de subproceso]** ~1-3s de arranque por invoke frente a ~200ms de la API. Mitigación: documentar; resume amortiza en conversaciones; batch paraleliza con asyncio.
- **[tool_choice forzado no es garantizable]** Instrucción + reintento puede aun así no llamar la tool. Mitigación: error explícito tras N reintentos, documentado como desviación de nivel B.
- **[ToS de Anthropic]** Uso programático de suscripción consumer es zona gris (igual que la lib vieja). Mitigación: heredar la sección legal del README viejo; recomendar API key/Commercial Terms para producción.
- **[Cuota de suscripción]** Sin coste por token pero sí rate limits Pro/Max. Mitigación: exponer `RateLimitEvent` en `response_metadata` cuando llegue; `max_budget_usd` disponible en modo agéntico.

## Migration Plan

No aplica migración de usuarios (paquete nuevo). Para usuarios de la lib vieja se documenta tabla de equivalencias (`ChatClaudeCode` → `ChatClaudeCli`) en el README. Rollback: n/a.

## Open Questions

- ~~¿`effort` acepta los cinco niveles de ChatAnthropic?~~ **Resuelto (S1)**: `EffortLevel = Literal['low','medium','high','xhigh','max']` — passthrough directo.
- ¿El CLI emite `RateLimitEvent` en modo no-interactivo? (afecta solo a observabilidad, no bloquea).
- Publicación en PyPI: ¿nombre `langchain-claude-cli` libre? Comprobar antes del primer release.
