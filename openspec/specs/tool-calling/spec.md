# tool-calling


### Requirement: bind_tools con patrón LangChain clásico
`bind_tools(tools)` SHALL aceptar `BaseTool`, dicts (formato Anthropic u OpenAI), clases Pydantic y callables. Las tools SHALL registrarse como servidor MCP in-process con hook `PreToolUse` que devuelve `permissionDecision: "defer"`, de modo que cuando el modelo decide llamar una tool el run se detiene SIN ejecutarla y el `deferred_tool_use` resultante se mapea a `AIMessage.tool_calls` (con el nombre des-namespaceado, sin prefijo `mcp__`).

#### Scenario: El modelo emite tool_calls sin ejecutar
- **WHEN** se invoca `llm.bind_tools([get_weather]).invoke("¿Qué tiempo hace en Tokio?")`
- **THEN** devuelve `AIMessage` con `tool_calls == [{"name": "get_weather", "args": {"city": "Tokio"}, "id": <str>}]` y la función Python `get_weather` NO se ha ejecutado

#### Scenario: Respuesta sin tool call
- **WHEN** se invoca el modelo con tools bound y un prompt que no requiere tools
- **THEN** devuelve un `AIMessage` de texto normal con `tool_calls == []`

### Requirement: Cierre del ciclo con ToolMessage
Cuando el historial de entrada contiene el `AIMessage(tool_calls)` previo seguido de `ToolMessage`(s), el modelo SHALL reanudar la sesión CLI original (vía prefix-cache) enviando los `tool_result` blocks correspondientes, y devolver la respuesta final sin pérdida de contexto.

#### Scenario: Ciclo completo de tool calling
- **WHEN** se invoca con `[HumanMessage, AIMessage(tool_calls=[weather]), ToolMessage(content="25°C", tool_call_id=...)]`
- **THEN** la respuesta final incorpora "25°C" y la sesión reanudada conserva el contexto del primer turno

#### Scenario: Compatibilidad con create_agent de LangGraph
- **WHEN** se usa `ChatClaudeCli` con tools en `create_agent`/`create_react_agent` y se ejecuta una pregunta que requiere una tool
- **THEN** el loop del agente completa: tool call → ejecución por el grafo → respuesta final correcta

### Requirement: tool_choice con degradación documentada
`tool_choice="auto"` o `None` SHALL ser passthrough. `tool_choice="any"` o nombre de tool específico SHALL implementarse como instrucción de sistema más validación: si la respuesta no contiene la tool call requerida, se reintenta una vez; si persiste, se lanza un error explícito. `strict=True` y `parallel_tool_calls` SHALL aceptarse emitiendo `ClaudeCliCompatWarning` sobre su semántica degradada.

#### Scenario: Forzar tool específica
- **WHEN** se llama `bind_tools([a, b], tool_choice="a")` con un prompt cualquiera
- **THEN** la respuesta contiene una tool call de `a`, o se lanza un error explícito tras el reintento

### Requirement: Convivencia con modo agéntico
Cuando el modelo tiene tools LangChain bound Y tools built-in del CLI habilitadas (modo agéntico), el hook defer SHALL matchear únicamente las tools del servidor MCP `lc` (`mcp__lc__*`), permitiendo que las tools built-in se ejecuten normalmente dentro del run.

#### Scenario: Mezcla de tools diferidas y ejecutadas
- **WHEN** un modelo agéntico con `builtin_tools=["Read"]` y `bind_tools([mi_tool])` recibe una tarea que usa ambas
- **THEN** `Read` se ejecuta dentro del run del CLI y `mi_tool` se devuelve como tool_call diferida sin ejecutarse
