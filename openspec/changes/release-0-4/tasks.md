# Tasks: release-0-4

## 1. Split del monolito (refactor puro)

- [x] 1.1 `_options.py`: mixin de construcción de opciones/MCP/guard + helpers module-level
- [x] 1.2 `_runner.py`: mixin de ejecución (arun_query, chat_result, sesiones, generate/agenerate) + helpers
- [x] 1.3 `_streaming.py`: mixin de streaming (astream/stream)
- [x] 1.4 `chat_models.py` reducido a la clase (campos, bind_tools, with_structured_output) heredando mixins
- [x] 1.5 Verificación: suites unit+cassette idénticas en verde, mypy strict, ruff, smoke live (1 invoke + 1 ciclo tools)

## 2. Replay del pool

- [ ] 2.1 `FakeClaudeSDKClient` + fixture `client_cassette` (replay) y grabación envolviendo el cliente real
- [ ] 2.2 Grabar cassettes del flujo persistente (warm-up + reuso multi-turn) y migrar los tests del pool
- [ ] 2.3 Suite en verde sin CLI

## 3. Interrupt stateless

- [x] 3.1 Registro de runs activos por instancia + `ClaudeCliInterruptedError`
- [x] 3.2 `interrupt()` generalizado (task.cancel vía call_soon_threadsafe; pool si existe)
- [x] 3.3 Tests: fake colgado + interrupt desde otro hilo (sin CLI), no-huérfanos, invoke posterior OK

## 4. Release

- [ ] 4.1 README/CHANGELOG; mypy+ruff+suite completa
- [ ] 4.2 Bump 0.4.0, tag, verificar PyPI
