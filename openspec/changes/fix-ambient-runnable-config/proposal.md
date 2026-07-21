# fix-ambient-runnable-config

## Why

La capability `session-management` promete dos cosas que **hoy no ocurren nunca**:

- *Scenario: Recuperación por thread_id* — reanudar por `thread_id` cuando el
  checkpointer recorta el historial.
- *Requirement: session_id explícito* — fijar la sesión vía
  `config={"configurable": {"session_id": ...}}`.

Ambas leen el config en `kwargs.get("config")` (`_runner.py:253,525`,
`_streaming.py:62,292`). Pero `BaseChatModel.ainvoke(input, config, ...)`
consume `config` como parámetro propio y **nunca lo reenvía a `**kwargs`**
(langchain-core `chat_models.py`, `ainvoke`/`invoke`). Y el rodeo natural
también falla:

```
llm.bind(config=cfg).ainvoke(msgs)
TypeError: BaseChatModel.ainvoke() got multiple values for argument 'config'
```

Medición con un `BaseChatModel` sonda que lee ambas vías desde `_generate`:

| escenario | `kwargs["config"]` | `ensure_config()` |
|---|---|---|
| `llm.invoke(msgs)` | `None` | `None` |
| `llm.invoke(msgs, config={"configurable": {"thread_id": "TID-42"}})` | `None` | `None` |
| `llm.invoke(...)` **dentro de un nodo LangGraph** | `None` | `"TID-42"` |

Conclusión: `kwargs["config"]` es inalcanzable desde la API pública. El código
de resolución es correcto y sus tests unitarios pasan — lo que está roto es el
**cableado**. `tests/unit_tests/test_sessions_v02.py:73` llama a
`SessionCache.resolve(msgs, thread_id=...)` directamente, así que el hueco no se
ve.

Impacto real (consumidor `locamala-mesh`, runtime LangGraph multiagente): toda
conversación cuyo checkpointer normaliza los `AIMessage` degrada a flatten en
cada turno. Además del coste y del ruido, el modelo imita el formato
`[User]:`/`[Assistant]:` del prompt aplanado y lo filtra en su respuesta.

Un segundo defecto sale a la luz en cuanto el cableado funcione: la clave
`thread:<id>` **no distingue perfil de ejecución**. En un grafo real es habitual
que varias instancias del modelo compartan `thread_id` con modelos distintos
(p. ej. un router haiku y un ejecutor sonnet sobre el mismo hilo). Sin
namespacing, una reanudaría la sesión de la otra: cruce de contextos silencioso,
justo lo que el *Scenario: Instancia compartida entre conversaciones* prohíbe.

## What Changes

- **Resolución de config ambiental**: el config efectivo pasa a ser
  `kwargs.get("config")` **o**, en su defecto, `ensure_config()` (contextvar de
  langchain-core). El kwarg explícito mantiene prioridad; los llamantes
  existentes no cambian.
- **Namespacing de la clave de thread**: `thread:<id>` pasa a
  `thread:<perfil>:<id>`, donde `<perfil>` es un digest de los atributos
  **estables** de ejecución (`model`, `cwd`, `builtin_tools`,
  `permission_mode`). **NO** incluye `system_prompt`: los runtimes lo recomponen
  por turno (fecha, memoria, skills) y meterlo destruiría toda continuidad.
- **Contract test en grafo real**: un test que compile un `StateGraph`, invoque
  el modelo desde un nodo y verifique `strategy="resume"`. Es el test que
  faltaba y el que habría cazado el defecto.
- Sin API nueva, sin cambios de firma, sin dependencias nuevas.

## Impact

- Specs: `session-management` (MODIFIED)
- Código: `_runner.py`, `_streaming.py`, `_sessions.py`
- Compatibilidad: los llamantes que hoy pasan `config` por kwarg explícito
  siguen igual. El resto **gana** el comportamiento que la spec ya prometía.
- Riesgo de activación: un consumidor con `session_store="file"` y `thread_id`
  ambiental pasa de "siempre flatten" a "reanuda". Es el objetivo del cambio,
  pero cambia el camino caliente → el namespacing (R2) es la salvaguarda contra
  el cruce de sesiones, y debe entrar en el mismo release.
- Los turnos de usar-y-tirar (heartbeats, crons) conservan el opt-out sin API
  nueva: `session_store="memory"` es per-instancia, así que una instancia nueva
  por turno nunca reanuda.
