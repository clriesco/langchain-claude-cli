# replay-transport (delta v0.4)

## ADDED Requirements

### Requirement: Replay del cliente persistente
El harness SHALL cubrir también el camino `ClaudeSDKClient`: una fixture que sustituye el cliente por un doble reproduciendo exchanges grabados (connect/query/receive_response/interrupt), de modo que los flujos del pool (fast-path de reuso, warm-up, degradación por firma o TTL) se testeen sin CLI ni cuota.

#### Scenario: Fast-path del pool sin CLI
- **WHEN** se ejecuta el test de reuso multi-turn con `persistent=True` y cassette de cliente
- **THEN** el test verifica el hit del pool y la respuesta correcta sin proceso `claude`
