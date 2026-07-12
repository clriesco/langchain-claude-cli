# replay-transport


### Requirement: Reproducción determinista de streams grabados
El repositorio SHALL proveer un harness de cassettes: una fixture pytest que intercepta `claude_agent_sdk.query` y reproduce, por cada llamada, el siguiente intercambio grabado de `tests/cassettes/<nombre>.json`, reconstruyendo los tipos reales del SDK (`AssistantMessage`, `ResultMessage`, `StreamEvent`, `RateLimitEvent`). Los tests con cassette SHALL ejecutarse sin CLI, sin red y sin consumir cuota.

#### Scenario: Test E2E sin CLI
- **WHEN** se ejecuta un test migrado a cassette en una máquina sin `claude` instalado
- **THEN** el test pasa reproduciendo el stream grabado, en menos de un segundo

#### Scenario: Ciclo multi-llamada
- **WHEN** un test de ciclo de tool calling ejecuta dos invokes (defer + entrega)
- **THEN** la cassette reproduce ambos intercambios en orden y el segundo invoke recibe el stream del resume

### Requirement: Modo grabación
Con `RECORD_CASSETTES=1`, el harness SHALL delegar en el `query` real, coleccionar los mensajes emitidos y volcarlos a la cassette del test junto a metadatos de la request (modelo, nº de entries, presencia de tools). En replay, el harness SHALL validar ese matching laxo y fallar con mensaje claro si la request no corresponde a la grabación.

#### Scenario: Regrabación de una cassette
- **WHEN** se ejecuta el test con `RECORD_CASSETTES=1` y CLI autenticado
- **THEN** la cassette se sobreescribe con el stream real y el replay posterior reproduce el nuevo contenido

#### Scenario: Desalineación request/grabación
- **WHEN** un test con cassette envía una request cuyo modelo no coincide con el grabado
- **THEN** el harness falla explícitamente indicando la discrepancia (no reproduce datos incoherentes)

### Requirement: Replay del cliente persistente
El harness SHALL cubrir también el camino `ClaudeSDKClient`: una fixture que sustituye el cliente por un doble reproduciendo exchanges grabados (connect/query/receive_response/interrupt), de modo que los flujos del pool (fast-path de reuso, warm-up, degradación por firma o TTL) se testeen sin CLI ni cuota.

#### Scenario: Fast-path del pool sin CLI
- **WHEN** se ejecuta el test de reuso multi-turn con `persistent=True` y cassette de cliente
- **THEN** el test verifica el hit del pool y la respuesta correcta sin proceso `claude`
