# structured-output

## ADDED Requirements

### Requirement: with_structured_output sobre output_format nativo
`with_structured_output(schema)` SHALL aceptar clases Pydantic, TypedDicts, JSON Schema y schemas de tool Anthropic/OpenAI. Con `method="json_schema"` (default) SHALL pasar el schema al SDK vía `options.output_format` y construir el resultado desde `ResultMessage.structured_output`. Si el schema es una clase Pydantic, el resultado SHALL ser una instancia validada; en otro caso, un dict.

#### Scenario: Schema Pydantic
- **WHEN** se llama `llm.with_structured_output(Answer).invoke("¿Capital de Francia?")` donde `Answer` es un BaseModel con campos `answer: str` y `confidence: float`
- **THEN** devuelve una instancia de `Answer` con `answer == "Paris"` y `confidence` float válido

#### Scenario: JSON Schema dict
- **WHEN** se pasa un dict JSON Schema como schema
- **THEN** devuelve un dict conforme al schema, sin validación Pydantic

### Requirement: method function_calling por compatibilidad
Con `method="function_calling"` el schema SHALL exponerse como tool diferida (mecanismo defer de tool-calling) y el resultado parsearse desde los `args` de la tool call, replicando la semántica de ChatAnthropic para código que fija ese método.

#### Scenario: function_calling explícito
- **WHEN** se llama `with_structured_output(Answer, method="function_calling").invoke(...)`
- **THEN** devuelve una instancia de `Answer` equivalente a la del método json_schema

### Requirement: include_raw
Con `include_raw=True` el resultado SHALL ser un dict `{"raw": AIMessage, "parsed": <obj|None>, "parsing_error": <Exception|None>}`; los errores de parseo SHALL capturarse en `parsing_error` en lugar de propagarse.

#### Scenario: Parseo fallido con include_raw
- **WHEN** el modelo devuelve una salida que no valida contra el schema e `include_raw=True`
- **THEN** `parsed` es `None`, `parsing_error` contiene la excepción y `raw` conserva el `AIMessage` original
