# Proposal: release-0-3

## Why

v0.2 está publicada y con usuarios reales, y la sesión de v0.2 dejó tres lecciones de fiabilidad: (1) los tests de integración dependen del CLI vivo — queman cuota de suscripción y bajo rate limit producen falsos negativos que cuestan horas de diagnóstico; (2) el modo por defecto de la librería ante un CLI muerto a mitad de run sigue siendo colgarse indefinidamente (mitigado solo si el usuario fija `timeout`); (3) dependemos de comportamientos finos del binario `claude`, que se auto-actualiza con independencia de nuestros pins — un cambio del CLI puede rompernos sin que cambie ninguna dependencia Python.

v0.3 ataca los tres en este orden: tests deterministas primero (abaratan verificar todo lo demás), watchdog de vida del run después, y tests de contrato nightly al final.

## What Changes

- **Record/replay de streams del SDK ("cassettes")**: fixture de pytest que intercepta `claude_agent_sdk.query` y reproduce streams grabados (JSON en `tests/cassettes/`). Modo grabación opt-in (`RECORD_CASSETTES=1`) contra el CLI real. La mayoría de la suite E2E pasa a ser determinista, rápida y sin cuota; queda una suite live mínima (smoke + contrato).
- **Watchdog de vida del run**: parámetro `inactivity_timeout` — si el stream del SDK no produce ningún mensaje en N segundos, el run se aborta con `ClaudeCliError` (cerrando el stream para no dejar huérfanos). Defaults conservadores decididos por spike (los turnos agénticos con tools lentas producen silencios legítimos). Logging namespaced (`langchain_claude_cli`) de los eventos clave (resolución de sesión, pool, reintentos, defer/delivery, watchdog) para diagnosticar sin arqueología.
- **Tests de contrato nightly**: los spikes S1-S9 convertidos en suite `contract` ejecutable contra el CLI del sistema, con workflow nightly (`workflow_dispatch` + cron) autenticado vía repo secret (`auth="inherit"` + API key, coste por token de céntimos) para detectar roturas de comportamiento del CLI antes que los usuarios.
- Sin breaking changes; `inactivity_timeout` es opt-out-able.

## Capabilities

### New Capabilities

- `replay-transport`: harness de cassettes — grabación y reproducción determinista de streams del SDK para tests.
- `contract-tests`: suite de contrato CLI↔librería y su ejecución programada.

### Modified Capabilities

- `chat-model-core`: watchdog de inactividad (`inactivity_timeout`) con cierre limpio del stream; logging estructurado de eventos internos.

## Impact

- **Código**: `tests/_cassettes.py` (harness) + `tests/cassettes/*.json`; `chat_models.py` (watchdog + logging); `spikes/` → `tests/contract_tests/`; workflow `contract.yml`.
- **Dependencias**: ninguna nueva en runtime; el harness usa stdlib.
- **CI**: job de contrato nightly requiere secret `ANTHROPIC_API_KEY` en el repo (decisión del mantenedor; documentado coste estimado).
