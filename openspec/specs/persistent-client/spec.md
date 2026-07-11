# persistent-client


### Requirement: Modo cliente persistente opt-in
Con `ChatClaudeCli(persistent=True)`, el modelo SHALL mantener un pool thread-safe de `ClaudeSDKClient` keyed por `session_id` (tamaño y TTL configurables, defaults 4 clientes / 300s). Un invoke cuya resolución es `resume` con cliente vivo SHALL reutilizarlo (sin arrancar subproceso); en cualquier otro caso SHALL seguir el camino v0.1 y registrar el cliente al terminar. Con `persistent=False` (default) el comportamiento SHALL ser idéntico a v0.1.

#### Scenario: Multi-turn sin re-arranque
- **WHEN** una conversación con `persistent=True` encadena tres invokes consecutivos
- **THEN** el segundo y tercero reutilizan el cliente vivo y su latencia media es medible y significativamente menor que la del primero

#### Scenario: Default intacto
- **WHEN** se construye el modelo sin `persistent`
- **THEN** no se crea ningún pool y el flujo es el de v0.1

### Requirement: Lifecycle y limpieza
La evicción del pool (LRU o TTL expirado) SHALL cerrar el cliente con `disconnect()`. La destrucción de la instancia del modelo y la salida del proceso SHALL cerrar el pool best-effort. El agotamiento del pool nunca SHALL bloquear un invoke: se degrada al camino stateless.

#### Scenario: Evicción por capacidad
- **WHEN** el pool está lleno y una conversación nueva registra su cliente
- **THEN** el cliente menos recientemente usado se cierra con disconnect() y el nuevo ocupa su lugar

### Requirement: Interrupt y cambio de modelo en caliente
El modelo SHALL exponer `interrupt(session_id=None)` — cancela el run activo del cliente de esa sesión (o del último activo) — y el cambio de modelo en caliente sobre el cliente persistente de una conversación. Tras un interrupt, el siguiente invoke de la conversación SHALL funcionar (estado pendiente re-manejado por la mecánica de resume existente).

#### Scenario: Interrupt de una generación larga
- **WHEN** se lanza un invoke largo en un hilo y se llama `interrupt()` desde otro
- **THEN** la generación termina anticipadamente sin colgar el proceso y el siguiente invoke de esa conversación funciona
