# Proposal: create-langchain-claude-cli

## Why

`langchain-claude-code` (la única integración LangChain↔Claude Code existente) está construida sobre `claude-code-sdk` 0.0.20, un SDK muerto (tope 0.0.25) cuyo sucesor `claude-agent-sdk` va por 0.2.115. Sus features clave son hacks: `bind_tools` inyecta schemas en el system prompt y parsea JSON a mano, el extended thinking es texto añadido al prompt, y el historial multi-turn se aplana a un string perdiendo fidelidad. El SDK actual ofrece primitivos nativos para todo eso (MCP in-process, `output_format`, `thinking` config, hook `defer`, sesiones con resume/fork).

Queremos `langchain-claude-cli`: una librería Python nueva que sea **drop-in replacement de `ChatAnthropic`** (langchain-anthropic 1.4.x) usando la suscripción Claude Pro/Max vía Claude Code CLI — sin API key. Todo lo que se puede hacer por API debe poder hacerse por esta librería: nativo donde el SDK lo soporte, workaround donde no, y no-op documentado donde sea imposible.

## What Changes

- Nuevo paquete Python `langchain-claude-cli` (módulo `langchain_claude_cli`) con la clase `ChatClaudeCli(BaseChatModel)`, construido sobre `claude-agent-sdk >= 0.2.x`.
- **Paridad de superficie con `ChatAnthropic`** clasificada en tres niveles:
  - **Nivel A (nativo)**: invoke/stream/batch (sync+async), system messages, imágenes, tool calling clásico (el modelo devuelve `AIMessage.tool_calls` sin ejecutar), `with_structured_output` vía `output_format` nativo, `thinking` (formato idéntico a ChatAnthropic), `effort`, `usage_metadata` real (incl. cache tokens y `total_cost_usd`), `stop_reason`, MCP servers, server tools (web search/fetch → tools built-in del CLI), retries/timeout/fallback_model.
  - **Nivel B (workaround)**: `tool_choice` forzado (instrucción + validación/reintento), `stop_sequences` (truncado cliente + interrupt), `max_tokens` (truncado cliente), `get_num_tokens_from_messages` (estimación heurística), historial multi-turn arbitrario (session prefix-cache con resume; fallback a flatten), PDFs/documents (según spike), `parallel_tool_calls` (secuencial si defer es singular).
  - **Nivel C (no-op documentado)**: `temperature`/`top_k`/`top_p`, `citations`, computer use, `anthropic_proxy`/`default_headers`/`api_url`, `inference_geo`, `cache_control` (el CLI cachea solo). Los parámetros se aceptan sin romper y emiten warning una vez.
- **Tool calling clásico vía mecanismo `defer`**: tools LangChain registradas como servidor MCP in-process + hook `PreToolUse` que devuelve `"defer"` → el run se detiene y `ResultMessage.deferred_tool_use` se mapea a `AIMessage.tool_calls`. El ciclo se cierra reanudando la sesión con el `tool_result`. Compatible con `create_agent`/`create_react_agent` de LangGraph.
- **Modo LLM puro por defecto**: `tools=[]` (sin herramientas built-in, sin acceso a filesystem — semántica idéntica a la API). Modo agéntico opt-in con las capacidades de la librería vieja (`max_turns`, `allowed_tools`/`disallowed_tools`, `permission_mode`, `cwd`, enum de tools y presets).
- **Gestión de sesiones**: prefix-cache que mapea prefijos de historial a `session_id` del CLI para reanudar sesiones enviando solo el sufijo nuevo, preservando fidelidad total de mensajes.
- **5 spikes de validación** previos al grueso de la implementación: (1) round-trip defer→resume con tool_result, (2) defer con tool calls paralelas, (3) replay de mensajes assistant en stream-json input, (4) document/PDF blocks en input, (5) fidelidad de streaming (deltas de thinking y tool-input).
- Fuera de alcance v0.1: réplica del módulo `middleware/` de langchain-anthropic (redundante — el CLI trae bash/editor/memoria/búsqueda nativos), computer use, citations reales.

## Capabilities

### New Capabilities

- `chat-model-core`: Clase `ChatClaudeCli` con el contrato `BaseChatModel` completo — invoke/ainvoke/stream/astream/batch, conversión de mensajes LangChain↔CLI con fidelidad total (system, human, AI, tool, multimodal), `usage_metadata`, `response_metadata` (stop_reason, session_id, coste), y compat de firma con `ChatAnthropic` (niveles A/B/C).
- `tool-calling`: `bind_tools` con patrón LangChain clásico vía MCP in-process + hook defer; soporte de `tool_choice`, ciclo tool_call→ToolMessage→resume, e integración con LangGraph.
- `structured-output`: `with_structured_output` sobre `output_format` nativo del SDK, con métodos `function_calling` y `json_schema`, `include_raw`, y validación Pydantic.
- `session-management`: prefix-cache de sesiones, resume/fork, `continue_conversation`, y estrategia de fallback para historial arbitrario.
- `agentic-mode`: modo agéntico opt-in — tools built-in del CLI, permisos, presets de seguridad, working directory; paridad con los features de la librería vieja.
- `streaming`: streaming token-a-token real (texto, thinking, tool-input deltas) vía `StreamEvent`, sync y async, con `stop_sequences` client-side.

### Modified Capabilities

(ninguna — proyecto greenfield, no hay specs existentes)

## Impact

- **Código nuevo**: paquete completo en `libs/claude-cli/` (o raíz del repo — decidir en design): `chat_models.py`, `tools.py`, conversión de mensajes, session cache, tests unit + integración.
- **Dependencias**: `langchain-core` (target 1.x, rango a decidir en design), `claude-agent-sdk >= 0.2.x`. Requiere CLI `claude` instalado y autenticado (Pro/Max) en runtime.
- **Sistemas**: subproceso `claude` por invocación (o sesión reanudada); consume cuota de suscripción del usuario.
- **Referencia**: el repo vendorizado `langchain-claude-code/` sirve solo como referencia de features; no se modifica ni se depende de él.
