# chat-model-core (delta v0.3)

## ADDED Requirements

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
