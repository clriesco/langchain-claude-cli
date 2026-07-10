# Spike Findings

Entorno: claude CLI 2.1.206 · claude-agent-sdk 0.2.115 · modelo de prueba claude-haiku-4-5.

## S1 — Round-trip defer (`s1_defer.py`, `s1b_resume.py`) ✅ PASSED (con ajuste de diseño)

**Fase 1 (defer)**: funciona exactamente como D3. MCP in-process + hook `PreToolUse` → `"defer"`:
- La tool NO se ejecuta; el run se detiene con `stop_reason="tool_deferred"` (subtype `success`, `is_error=False`).
- `ResultMessage.deferred_tool_use` trae `{id, name, input}` (name con namespace `mcp__lc__...`).
- El modelo emite además un TextBlock post-defer ("I attempted... tool encountered...") — **descartar ese texto** al construir el `AIMessage(tool_calls)`, o exponerlo como contenido separado.

**Fase 2 (resume) — AJUSTE DE DISEÑO sobre D3.5**: enviar un `tool_result` block como mensaje de usuario NO funciona (el CLI lo trata como mensaje vacío). El mecanismo correcto:
- Al reanudar (`resume=session_id`), el CLI **re-dispara automáticamente** la tool call pendiente contra el servidor MCP.
- La entrega del resultado = el handler MCP devuelve el contenido del `ToolMessage` almacenado.
- En la pata de resume NO se registra hook defer (o el hook permite esa tool ya resuelta).
- Prompt de resume: stream vacío (la tool pendiente conduce el turno). Funciona sin timeout.
- ⚠️ El handler se invocó 2 veces en el resume → los handlers de entrega deben ser **idempotentes** (devolver siempre el mismo resultado almacenado).

**Implicación para `chat_models`/`tool-calling`**: `bind_tools` registra handlers que leen de un mapa `tool_call_id → resultado` poblado desde los `ToolMessage` del sufijo; el hook defer solo se instala cuando NO hay resultados pendientes que entregar (o discrimina por id).

**Bonus verificado**: `EffortLevel = Literal['low','medium','high','xhigh','max']` — paridad exacta con los 5 niveles de ChatAnthropic. Sin mapeo necesario.

## S2 — Defer con múltiples tool calls (`s2_parallel.py`) ✅ PASSED

- El modelo emite N `ToolUseBlock` en un turno; el hook difiere **todas** (N disparos).
- `ResultMessage.deferred_tool_use` (singular) solo lleva la **última** → **irrelevante**: los `AssistantMessage` del stream contienen todos los `ToolUseBlock` con `{id, name, input}`. `AIMessage.tool_calls` se construye desde los bloques, no desde el campo singular.
- Al reanudar, el CLI re-dispara **todas** las tool calls pendientes contra el handler; la respuesta final integra todos los resultados.
- **`parallel_tool_calls` soportado nativamente.** Sin degradación a secuencial.

## S3 — Replay de mensajes assistant (`s3_replay.py`) ✅ PASSED con coste

- El input stream-json **acepta mensajes `assistant` fabricados y los honra** (el modelo respondió con el dato inventado del turno assistant inyectado).
- ⚠️ Coste: **cada mensaje de usuario histórico dispara una generación en vivo** (el CLI respondió también al primer mensaje histórico). Replay fiel cuesta O(turnos_user históricos) generaciones.
- Implicación para D4: el replay multi-mensaje NO es un fallback barato. Fallback por defecto = **structured flatten**: todo el historial en UN solo mensaje de usuario cuyos content blocks preservan imágenes/documents y etiquetan roles en texto → una sola generación, multimodal intacto. Mejor que el flatten a string de la lib vieja.

## S4 — Document/PDF blocks (`s4_document.py`) ✅ PASSED

- Bloques `{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", ...}}` funcionan nativos en el input; el modelo lee el contenido del PDF.
- **PDFs suben del nivel B al nivel A** (nativo) en la matriz de paridad.

## S5 — Fidelidad de streaming (`s5_streaming.py`) ✅ PASSED

Con `include_partial_messages=True`, `StreamEvent.event` trae el stream raw de la API completo:
- `content_block_delta` con `text_delta`, `thinking_delta`, `signature_delta`, `input_json_delta`.
- `content_block_start` (text/thinking/tool_use), `content_block_stop`, `message_start/delta/stop`.
- Suficiente para `AIMessageChunk` con texto, thinking blocks y `tool_call_chunks` (D7 confirmado sin cambios).

## Ajustes de diseño derivados (aplicados en design.md)

1. **D3.5**: entrega de tool results = handler MCP en el resume (re-disparo automático), no `tool_result` como mensaje de usuario. Handlers idempotentes (se observó doble invocación).
2. **D3.4**: `tool_calls` desde los `ToolUseBlock` de los `AssistantMessage` (todos), no desde `deferred_tool_use` singular. Texto posterior al primer tool_use diferido se descarta (es la reacción del modelo al defer, ruido).
3. **D3/tool_choice**: `parallel_tool_calls` nativo — solo `strict` queda degradado.
4. **D4 rama 2**: fallback para historial arbitrario = structured flatten (un solo user message multimodal); replay multi-mensaje disponible pero costoso, no default.
5. **D6**: `effort` sin mapeo — los 5 niveles coinciden exactamente.
