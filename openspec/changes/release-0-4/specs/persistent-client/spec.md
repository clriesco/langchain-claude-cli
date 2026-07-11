# persistent-client (delta v0.4)

## MODIFIED Requirements

### Requirement: Interrupt y cambio de modelo en caliente
El modelo SHALL exponer `interrupt(session_id=None)` para CUALQUIER modo: con cliente persistente vivo, cancela su run activo vía el protocolo del CLI; en modo stateless, cancela el task del invoke activo (todos los de la instancia si no se especifica `session_id`), cerrando el stream sin dejar subprocesos huérfanos y haciendo que el invoke cancelado lance `ClaudeCliInterruptedError`. El cambio de modelo en caliente (`set_session_model`) SHALL seguir requiriendo cliente persistente. Tras un interrupt, el siguiente invoke de la conversación SHALL funcionar.

#### Scenario: Interrupt de una generación larga (persistente)
- **WHEN** se lanza un invoke largo con `persistent=True` en un hilo y se llama `interrupt()` desde otro
- **THEN** la generación termina anticipadamente sin colgar el proceso y el siguiente invoke de esa conversación funciona

#### Scenario: Interrupt en modo stateless
- **WHEN** se lanza un invoke stateless largo en un hilo y se llama `interrupt()` desde otro
- **THEN** el invoke lanza `ClaudeCliInterruptedError` en segundos, no queda proceso `claude` huérfano, y un invoke posterior funciona
