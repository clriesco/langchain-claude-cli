# Tasks: create-langchain-claude-cli

## 1. Setup del proyecto

- [x] 1.1 Crear `pyproject.toml` en el raíz (paquete `langchain-claude-cli`, módulo `langchain_claude_cli`, deps: `langchain-core>=1.0,<2`, `claude-agent-sdk>=0.2.115,<0.3`; dev: pytest, pytest-asyncio, ruff, mypy, langchain-tests, langgraph) con config de ruff/mypy basada en la de la lib de referencia
- [x] 1.2 Crear esqueleto del paquete: `langchain_claude_cli/{__init__.py, chat_models.py, _convert.py, _sessions.py, _compat.py, tools.py}` y `tests/{unit_tests,integration_tests}`
- [x] 1.3 Verificar entorno: CLI `claude` autenticado y `python -c "import claude_agent_sdk"` funcionando

## 2. Spikes de validación (bloqueantes del diseño)

- [x] 2.1 S1 — Round-trip defer: script `spikes/s1_defer.py` que registra tool MCP in-process + hook PreToolUse→defer, verifica que `ResultMessage.deferred_tool_use` llega sin ejecutar la tool, y que resume de la sesión + tool_result block produce respuesta final coherente. De paso verificar valores válidos de `effort`. Escribir conclusión en `spikes/FINDINGS.md`
- [x] 2.2 S2 — Defer con múltiples tool calls en un turno: determinar si se difiere más de una (campo singular) → decide semántica de `parallel_tool_calls`
- [x] 2.3 S3 — Replay de historial: probar si el input stream-json acepta mensajes assistant (con y sin tool_use blocks) → decide rama 2 vs 3 de la cascada de sesiones
- [x] 2.4 S4 — Document/PDF blocks en input stream-json → decide soporte de documentos en `_convert.py`
- [x] 2.5 S5 — Fidelidad de streaming: confirmar deltas de texto, thinking e input_json en `StreamEvent` con `include_partial_messages=True`
- [x] 2.6 Revisar FINDINGS y ajustar design.md si algún spike contradice una decisión (especialmente D3/D4)

## 3. Conversión de mensajes (`_convert.py`)

- [x] 3.1 LangChain→CLI: system/human/AI/tool messages a content blocks (texto, imagen base64/URL, tool_use, tool_result; documents según S4); ignorar `cache_control` silenciosamente
- [x] 3.2 CLI→LangChain: `TextBlock`/`ThinkingBlock`/`ToolUseBlock` a content blocks v1 + `tool_calls`; `usage`/`model_usage` a `usage_metadata` (incl. cache tokens); `stop_reason`/`session_id`/`total_cost_usd`/`model` a `response_metadata`
- [x] 3.3 Tests unitarios de conversión ida y vuelta (sin CLI, con fixtures de mensajes SDK)

## 4. Núcleo del chat model (`chat_models.py`)

- [x] 4.1 `ChatClaudeCli(BaseChatModel)`: campos con paridad de firma ChatAnthropic + campos propios de CLI; `_build_options()` que construye `ClaudeAgentOptions` (modo LLM puro por defecto: `tools=[]`, `max_turns=1`)
- [x] 4.2 Capa de compat (`_compat.py`): `ClaudeCliCompatWarning`, registro de warnings únicos por proceso, tabla de parámetros no-op (nivel C)
- [x] 4.3 `_agenerate` sobre `query()` con conversión de mensajes y metadata completa; `_generate` sync vía runner de event loop seguro
- [x] 4.4 Workarounds nivel B: `max_retries` (loop sobre `api_error_status`), `timeout`, `max_tokens` (truncado + stop_reason sintético), `get_num_tokens_from_messages` heurístico
- [x] 4.5 Tests unitarios con SDK mockeado (paridad de firma, warnings, retries, truncados)

## 5. Sesiones (`_sessions.py`)

- [x] 5.1 Fingerprint de historial (contenido normalizado, ignora metadata volátil) + caché LRU thread-safe prefijo→session_id
- [x] 5.2 Resolución en cascada: prefix match→resume con sufijo; historial arbitrario→replay (si S3 ok) o flatten con warning
- [x] 5.3 `session_id` explícito vía `config.configurable` con prioridad sobre la caché
- [x] 5.4 Tests unitarios de fingerprint, LRU, cascada y aislamiento entre conversaciones intercaladas

## 6. Tool calling

- [x] 6.1 Conversión de tools (BaseTool/dict/Pydantic/callable → schema vía `convert_to_openai_tool`) y construcción del servidor MCP in-process `lc` con handlers no-ejecutables
- [x] 6.2 Hook `PreToolUse` defer con matcher `mcp__lc__.*`; mapeo `deferred_tool_use` → `AIMessage.tool_calls` des-namespaceado
- [x] 6.3 Cierre del ciclo: detección de `ToolMessage`(s) en el sufijo → resume + tool_result blocks
- [x] 6.4 `tool_choice` ("auto"/"any"/nombre) con instrucción+validación+reintento; warnings para `strict` y `parallel_tool_calls` según S2
- [x] 6.5 Mapeo de server tools de Anthropic (`web_search_*`, `web_fetch_*`) a tools built-in del CLI
- [x] 6.6 Tests de integración: emisión de tool_calls, ciclo completo, y agente `create_agent`/`create_react_agent` end-to-end

## 7. Structured output

- [x] 7.1 `with_structured_output` con `method="json_schema"` sobre `output_format` + `ResultMessage.structured_output`; validación Pydantic
- [x] 7.2 `method="function_calling"` sobre el mecanismo defer; `include_raw` con captura de `parsing_error`
- [x] 7.3 Tests de integración con Pydantic, TypedDict y JSON Schema dict

## 8. Streaming

- [x] 8.1 `_astream`: traducción de `StreamEvent` raw → `AIMessageChunk` (texto, thinking blocks, tool_call_chunks) + chunk final con usage/response_metadata
- [x] 8.2 `_stream` sync con patrón thread+queue (seguro dentro de event loop existente)
- [x] 8.3 `stop_sequences` en streaming: buffer de detección, truncado y cancelación de la generación
- [x] 8.4 Tests de integración de streaming (chunks múltiples, thinking, tool calls, usage final, stop sequences)

## 9. Modo agéntico (`tools.py` + opciones)

- [x] 9.1 Enum `ClaudeTool` + presets (`READ_ONLY_TOOLS`, `WRITE_TOOLS`, `NETWORK_TOOLS`, `SHELL_TOOLS`) + `normalize_tools` (paridad lib vieja, actualizado a tools actuales del CLI)
- [x] 9.2 Parámetros agénticos: `builtin_tools` (lista/preset), `max_turns`, `permission_mode`, `cwd`, `add_dirs`, `max_budget_usd`, `sandbox`; convivencia defer/builtin verificada
- [x] 9.3 Tests de integración agénticos (read-only, presupuesto, mezcla con tools LangChain)

## 10. Calidad y release

- [x] 10.1 Suite estándar `langchain-tests` (ChatModelUnitTests/ChatModelIntegrationTests) con xfails documentados de los niveles B/C
- [x] 10.2 README completo: quick start, matriz de paridad A/B/C con ChatAnthropic, tabla de migración desde `langchain-claude-code`, sección de seguridad y de ToS (heredadas y actualizadas)
- [x] 10.3 CI (GitHub Actions): lint + mypy + unit tests; job de integración manual/nightly contra CLI real
- [ ] 10.4 Verificar disponibilidad del nombre `langchain-claude-cli` en PyPI, empaquetar y publicar 0.1.0; eliminar el clon de referencia `langchain-claude-code/` del repo
