# agent-middleware

## ADDED Requirements

### Requirement: ClaudeCodeToolsMiddleware para create_agent
El paquete SHALL exponer `langchain_claude_cli.middleware.ClaudeCodeToolsMiddleware`, un `AgentMiddleware` de LangChain 1.x que registra una tool (nombre default `claude_code`) en el agente. Su handler SHALL ejecutar la tarea recibida como run agéntico de Claude Code con la configuración del middleware (`builtin_tools`, `cwd`, `permission_mode`, `sandbox`, `max_budget_usd`, `model`) y devolver el resultado final como string. El middleware SHALL funcionar con cualquier chat model orquestador, no solo `ChatClaudeCli`.

#### Scenario: Agente delega trabajo de filesystem
- **WHEN** un `create_agent` con el middleware recibe una tarea que requiere leer archivos del workspace configurado
- **THEN** el modelo orquestador llama la tool `claude_code`, el run agéntico la resuelve dentro del CLI, y la respuesta final del agente incorpora el resultado

#### Scenario: Import sin langchain instalado
- **WHEN** se importa `langchain_claude_cli` en un entorno sin el paquete `langchain` (solo langchain-core)
- **THEN** el import del paquete principal no falla; solo `langchain_claude_cli.middleware` requiere `langchain`

### Requirement: Límites del run delegado
El middleware SHALL aplicar los límites configurados a cada ejecución de la tool: presupuesto (`max_budget_usd` → si se excede, la tool devuelve el error como resultado de tool en lugar de romper el grafo), sandbox y restricción de tools. La documentación SHALL advertir que cada ejecución consume cuota de la suscripción aunque el modelo orquestador sea de otro proveedor.

#### Scenario: Presupuesto excedido dentro de la tool
- **WHEN** la tarea delegada excede el `max_budget_usd` del middleware
- **THEN** el grafo no se rompe: la tool devuelve un mensaje de error de presupuesto y el agente puede continuar/responder
