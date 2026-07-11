# contract-tests


### Requirement: Suite de contrato CLI↔librería
El repositorio SHALL incluir `tests/contract_tests/` con los invariantes de comportamiento del CLI de los que depende la librería, derivados de los spikes: defer round-trip con re-disparo en resume, tool calls paralelas diferidas, replay de mensajes assistant, document blocks, formas del streaming (`text_delta`/`thinking_delta`/`input_json_delta`), niveles de `effort`, emisión de `RateLimitEvent`, y neutralización de `ANTHROPIC_API_KEY` por override vacío. Cada test SHALL nombrar el invariante y la decisión de diseño que lo consume.

#### Scenario: Rotura de contrato detectada
- **WHEN** una versión nueva del CLI deja de emitir `stop_reason="tool_deferred"` al diferir
- **THEN** el test de contrato correspondiente falla identificando el invariante roto (D3 de v0.1)

### Requirement: Ejecución programada
Un workflow (`contract.yml`) SHALL ejecutar la suite de contrato nightly (cron) y bajo demanda (`workflow_dispatch`), instalando el CLI y autenticándose con el secret `ANTHROPIC_API_KEY` del repositorio (`auth="inherit"`); cada test SHALL acotar coste (modelo haiku, `max_budget_usd`). Si el secret no existe (forks), el job SHALL saltarse con aviso en lugar de fallar.

#### Scenario: Nightly en el repo principal
- **WHEN** corre el cron con el secret configurado
- **THEN** la suite de contrato se ejecuta contra el CLI más reciente y su resultado queda visible en Actions

#### Scenario: Fork sin secret
- **WHEN** el workflow corre en un fork sin `ANTHROPIC_API_KEY`
- **THEN** el job termina en skipped/success con un aviso, sin error
