# Proposal: release-0-4

## Why

Deuda estructural señalada en la retro de v0.3: (1) `chat_models.py` es un monolito de ~1.400 líneas con seis responsabilidades — costoso para contribuidores externos (ya hay dos) y crece con cada release; (2) el camino `persistent=True` (ClaudeSDKClient) no está cubierto por el harness de cassettes — sus flujos solo tienen tests unitarios con fakes o integración en vivo; (3) `interrupt()` solo funciona en modo persistente — asimetría sorprendente para el usuario.

## What Changes

- **Split del monolito** (refactor puro, cero cambios de comportamiento): `chat_models.py` → clase + mixins en `_options.py` (construcción de opciones, MCP, guard), `_runner.py` (ejecución, retries, watchdog, sesiones) y `_streaming.py` (traducción de eventos). Las cassettes son la red de seguridad.
- **Replay para el pool**: `FakeClaudeSDKClient` en el harness — los flujos del cliente persistente (warm-up, reuso, degradación) pasan a tests deterministas.
- **`interrupt()` stateless**: cancelación del task del invoke activo (cierre limpio ya garantizado por el patrón aclose) con `ClaudeCliInterruptedError`; semántica de concurrencia definida en design.

## Capabilities

### New Capabilities

(ninguna)

### Modified Capabilities

- `replay-transport`: cobertura del camino ClaudeSDKClient (pool).
- `persistent-client`: `interrupt()` deja de requerir `persistent=True` (se generaliza a cualquier invoke activo).

## Impact

- Módulos nuevos `_options.py`/`_runner.py`/`_streaming.py`; API pública intacta.
- Harness de cassettes ampliado; tests nuevos del pool.
- Release 0.4.0.
