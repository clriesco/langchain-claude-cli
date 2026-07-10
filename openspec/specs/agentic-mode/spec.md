# agentic-mode


### Requirement: Activación opt-in del modo agéntico
El modo agéntico SHALL activarse explícitamente vía parámetros dedicados: `builtin_tools` (lista de nombres o preset `"claude_code"`), `max_turns > 1`, `permission_mode`, `cwd`, `add_dirs`, `max_budget_usd` y `sandbox`. Sin estos parámetros el modelo SHALL permanecer en modo LLM puro (`tools=[]`).

#### Scenario: Agente con filesystem
- **WHEN** se construye `ChatClaudeCli(builtin_tools="claude_code", max_turns=10, permission_mode="bypassPermissions", cwd="/proyecto")` y se invoca "lee main.py y corrige el bug"
- **THEN** el CLI ejecuta sus tools built-in (Read/Edit/...) dentro del run y devuelve el resultado final como `AIMessage`

#### Scenario: Presupuesto máximo
- **WHEN** se configura `max_budget_usd` y la tarea excede el presupuesto
- **THEN** el run se detiene y se lanza `ClaudeCliBudgetExceededError` inmediatamente, SIN consumir reintentos (verificado: el SDK aborta el stream con error terminal, no hay resultado parcial que reflejar)

### Requirement: Control de acceso a herramientas
`allowed_tools` y `disallowed_tools` SHALL aceptar strings y valores del enum `ClaudeTool` (re-exportado con presets `READ_ONLY_TOOLS`, `WRITE_TOOLS`, `NETWORK_TOOLS`, `SHELL_TOOLS`), normalizándose a nombres únicos. Paridad con la librería antigua.

#### Scenario: Agente read-only
- **WHEN** se construye con `builtin_tools=READ_ONLY_TOOLS, max_turns=5` y se pide modificar un archivo
- **THEN** el CLI no dispone de tools de escritura y la modificación no ocurre

### Requirement: Server tools de ChatAnthropic mapeadas a tools del CLI
Cuando se pasan a `bind_tools` schemas de server tools de Anthropic (`web_search_20250305`, `web_fetch_20250910`), el modelo SHALL habilitar las tools built-in equivalentes del CLI (`WebSearch`, `WebFetch`) en lugar de registrarlas como tools diferidas, documentando que la forma de los bloques de resultado difiere de la API.

#### Scenario: Web search
- **WHEN** se invoca `llm.bind_tools([{"type": "web_search_20250305", "name": "web_search"}]).invoke("¿último release de Python?")`
- **THEN** el CLI ejecuta WebSearch dentro del run y la respuesta final incluye información actual
