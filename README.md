# incident-root-cause-and-runbook-copilot

Copiloto de correlación de incidentes para plataformas de datos (pipelines ETL, bases
transaccionales/analíticas y servicios cloud). Agrupa señales de fallo en ventanas de
tiempo, identifica el cambio reciente más probable como causa raíz candidata, recupera
fragmentos de runbooks con citas exactas y genera un borrador de RCA auditable. El
proveedor de LLM es AWS Bedrock (Claude) o un modo `mock` determinístico para pruebas y demos.

Mantenido por DataOps-Solutions (dataopss.com).

## Arquitectura

```
 data/sample_signals.json          data/runbooks/*.md
           |                                |
           v                                v
 +--------------------+          +----------------------+
 | 1. Ingesta         |          | 4. Indexado BM25     |
 |    (Pydantic v2)   |          |    por sección "##"  |
 +--------------------+          +----------------------+
           |                                |
           v                                |
 +--------------------+                     |
 | SQLite / PostgreSQL|                     |
 | signals            |                     |
 | change_events      |                     |
 | component_lineage  |                     |
 +--------------------+                     |
           |                                |
           v                                |
 +--------------------+                     |
 | 2. Correlación     |                     |
 |    temporal +      |                     |
 |    linaje + cambios|                     |
 +--------------------+                     |
           |                                |
           v                                v
 +------------------------------------------------+
 | 3. Contexto del incidente + citas de runbook   |
 +------------------------------------------------+
           |
           v
 +------------------------------------------------+
 | 5. Agente RCA                                  |
 |    prompt -> LLM (mock | bedrock)             |
 |    validación JSON contra RCAModelOutput       |
 |    guardrails (solo lectura, aprobación humana)|
 +------------------------------------------------+
           |
           v
 output/rca_report.json   output/rca_report.md   (+ tabla rca_reports)
```

Módulos:

| Módulo | Responsabilidad |
| --- | --- |
| `src/models.py` | Contratos Pydantic v2: señales, cambios, linaje, incidentes, citas y salida del RCA |
| `src/correlator.py` | Ingesta idempotente, reglas de correlación deterministas, persistencia |
| `src/runbook_retriever.py` | Índice BM25 sin dependencias externas; devuelve archivo, sección y cita textual |
| `src/rca_agent.py` | Construcción del prompt, providers `mock`/`bedrock`, validación y guardrails |
| `src/main.py` | CLI end-to-end y renderizado de reportes |
| `sql/schema.sql` | DDL portable (SQLite y PostgreSQL 14+) |

## Flujo paso a paso

1. **Ingesta.** `Dataset` valida el JSON de entrada. Cada señal tiene `signal_type`
   (`alert`, `metric`, `log_error`), severidad y timestamp UTC obligatorio. Los datos se
   guardan en SQLite con `INSERT OR REPLACE`, por lo que volver a ingerir no duplica filas.
2. **Correlación.** Las reglas son deterministas:
   - Una señal se une a un cluster abierto si ocurre dentro de `--window-minutes` (30 por
     defecto) de la última señal del cluster **y** su componente es el mismo o está
     conectado por linaje de datos.
   - Cada cluster recibe reglas disparadas: `temporal_cluster`, `lineage_linked`,
     `precursor_degradation`, `recent_change`, `critical_alert`.
   - El cambio candidato es el más reciente en el componente del cluster o en sus upstreams,
     dentro de `--lookback-minutes` (120 por defecto) antes del primer síntoma.
   - El score es `0.15` por regla disparada, más `0.2` si el cambio ocurrió hasta 60 minutos
     antes del primer síntoma, con tope en 1.0.
3. **Recuperación de runbooks.** Cada sección `## ` de `data/runbooks/*.md` es un documento.
   La consulta combina componentes, nombres de señal, mensajes y la descripción del cambio.
   La cita devuelta es un bloque de líneas consecutivas tomado del archivo original, así que
   puede verificarse con una comparación de subcadenas.
4. **Prompt.** Se envían al LLM el incidente, las señales, los cambios, las citas y el JSON
   Schema de salida. El prompt marca el contenido de los runbooks como dato no confiable.
5. **Validación.** La respuesta debe ser un objeto JSON que cumpla `RCAModelOutput`
   (`extra="forbid"`, rangos de confianza, `requires_human_approval: true` obligatorio).
   Una respuesta que no valida produce `RCAGenerationError` y no se persiste.
6. **Guardrails.** Antes de armar el reporte se filtran las sugerencias automáticas:
   - Las verificaciones deben empezar con `SELECT`, `WITH`, `EXPLAIN` o `SHOW`, contener una
     sola sentencia y no incluir palabras clave de escritura o efectos secundarios
     (`DROP`, `DELETE`, `TRUNCATE`, `ALTER`, `UPDATE`, `INSERT`, `GRANT`, `REVOKE`, `CREATE`,
     `MERGE`, `INTO`, `FOR UPDATE`, `pg_terminate_backend`, `set_config`, `nextval`, entre otras).
   - Las mitigaciones se describen en texto y no pueden contener términos destructivos.
     Siempre requieren aprobación humana.
   - Cada elemento descartado queda registrado en `guardrail_notes`.
7. **Salida.** Se escriben `output/rca_report.json` y `output/rca_report.md`, y el reporte
   se guarda en la tabla `rca_reports`.

## Ejecución local (3 comandos)

```bash
make setup    # crea .venv, instala dependencias y copia .env.example a .env
make test     # 36 tests: correlación, recuperación de citas y guardrails
make demo     # ejecuta el pipeline completo con LLM_PROVIDER=mock
```

`make demo` equivale a `LLM_PROVIDER=mock python -m src.main`. Los archivos quedan en `output/`.
Para usar Bedrock, copiar `.env.example` a `.env`, configurar `AWS_REGION`, `BEDROCK_MODEL_ID` y
las credenciales (perfil o rol IAM), y ejecutar con `LLM_PROVIDER=bedrock`. El modelo debe
estar habilitado en la cuenta y región elegidas.

Opciones de la CLI: `--dataset`, `--runbooks`, `--output`, `--provider`, `--window-minutes`,
`--lookback-minutes`.

## Casos de uso B2B

- **Guardia de datos.** Reducir el tiempo de triage cuando una alerta de latencia y un fallo
  de pipeline ocurren juntos, mostrando el cambio reciente y las citas del runbook correspondiente.
- **Post-mortems.** Dejar trazabilidad de qué señales y qué cambios se consideraron, con las
  reglas que dispararon cada correlación.
- **Gobierno de cambios.** Revisar en una misma vista los despliegues de pipelines que
  preceden a incidentes en bases compartidas.
- **Onboarding de operadores.** Presentar los runbooks relevantes junto con la evidencia del
  incidente activo, sin buscar manualmente en el wiki.

## Decisiones de diseño

- **Correlación determinista antes que IA.** La agrupación y la selección del cambio candidato
  son reglas explícitas y testeables. El LLM solo redacta la explicación sobre ese contexto.
  Esto hace que los resultados sean reproducibles y auditables.
- **Citas recuperadas, no generadas.** Las citas del reporte provienen del índice, nunca del LLM,
  para que no puedan inventarse fuentes.
- **BM25 propio.** Recuperación léxica implementada en pocas decenas de líneas, sin embeddings
  ni servicios externos. Es suficiente para runbooks con vocabulario técnico estable. Si el
  volumen crece, puede reemplazarse por embeddings detrás de la misma interfaz `search()`.
- **SQLite con DDL portable.** El esquema evita `AUTOINCREMENT` y tipos propios de SQLite, por
  lo que puede migrarse a PostgreSQL. Las columnas de listas son `TEXT` con JSON y pueden pasar
  a `JSONB`.
- **Mock como proveedor de primera clase.** Permite correr tests y demos sin credenciales. El
  mock recibe el mismo prompt tipado que Bedrock y su salida pasa por la misma validación.
- **Validación en dos capas.** El esquema rechaza formas inválidas (por ejemplo,
  `requires_human_approval: false`), y los guardrails eliminan contenido destructivo
  bien formado.

## Seguridad

- Ninguna acción de mitigación se ejecuta. El sistema solo genera texto para revisión humana.
- Las verificaciones automáticas son de solo lectura y se validan dos veces: al filtrar la salida
  del LLM y en los tests (`tests/test_rca_guardrails.py`).
- Las credenciales de AWS nunca se guardan en el repositorio. `.env` está en `.gitignore`.
- Los runbooks se tratan como datos no confiables dentro del prompt.
- Los datos sensibles de clientes no deben incluirse en `sample_signals.json`. Los datos de
  ejemplo son sintéticos.

## Estructura

```
.
├── Makefile
├── README.md
├── requirements.txt
├── pytest.ini
├── .env.example
├── sql/schema.sql
├── data/
│   ├── sample_signals.json
│   └── runbooks/
│       ├── db_high_latency_locks.md
│       └── etl_pipeline_failure.md
├── src/
│   ├── __init__.py
│   ├── models.py
│   ├── correlator.py
│   ├── runbook_retriever.py
│   ├── rca_agent.py
│   └── main.py
├── tests/
│   ├── conftest.py
│   ├── test_correlator.py
│   └── test_rca_guardrails.py
└── output/            # generado por `make demo`
```

## Limitaciones conocidas

- La correlación usa ventanas temporales y linaje directo. No infiere dependencias que no estén
  declaradas en `lineage`.
- Si no hay cambios en el alcance del incidente, la causa raíz candidata es el componente de la
  primera señal, una heurística explícita en el reporte.
- El modo `bedrock` no está cubierto por tests de integración contra AWS. Se prueba con un
  cliente simulado que respeta el formato de la API de Messages de Anthropic.
