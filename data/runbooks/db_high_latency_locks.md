# Runbook: alta latencia en PostgreSQL por bloqueos (locks)

Ámbito: base transaccional/analítica `orders-db` (PostgreSQL 14+).
Severidad típica: SEV-2 cuando la latencia p95 supera el umbral durante más de 10 minutos.

## Síntomas

- La latencia de lectura p95 supera el umbral definido para `orders-db`.
- Aparecen errores `canceling statement due to statement timeout` en el log de PostgreSQL.
- Crece el número de sesiones activas y la espera máxima de locks (`lock_wait_seconds_max`).
- La alarma de bloqueos (`DBLocksBlocked`) pasa a estado ALARM.

## Diagnóstico de solo lectura

Ejecutar únicamente sentencias de lectura. No usar credenciales de escritura para estas consultas.

1. Transacciones abiertas más antiguas, para identificar sesiones de larga duración:

   ```sql
   SELECT pid, usename, application_name, state,
          now() - xact_start AS transaction_age,
          left(query, 200) AS query_excerpt
   FROM pg_stat_activity
   WHERE xact_start IS NOT NULL
   ORDER BY xact_start
   LIMIT 20;
   ```

2. Sesiones bloqueadas y quién las bloquea (`pg_blocking_pids`):

   ```sql
   SELECT pid, pg_blocking_pids(pid) AS blocked_by,
          wait_event_type, wait_event,
          now() - query_start AS query_age
   FROM pg_stat_activity
   WHERE cardinality(pg_blocking_pids(pid)) > 0;
   ```

3. Relaciones con locks no concedidos:

   ```sql
   SELECT locktype, relation::regclass AS relation, mode, pid
   FROM pg_locks
   WHERE NOT granted
   ORDER BY pid;
   ```

4. Cambios recientes de esquema e índices sobre tablas afectadas (revisar el registro de migraciones, no consultar DDL en producción).

5. Correlacionar con despliegues: confirmar en el sistema de CI/CD si hubo un deploy de pipelines que escriben sobre la base en los 60 minutos previos al primer síntoma.

## Interpretación

- Una transacción abierta con `state = 'idle in transaction'` y antigüedad mayor a 5 minutos es la causa más probable de bloqueos en cadena.
- Si el bloqueador pertenece a un proceso ETL, el hallazgo debe cruzarse con el runbook `etl_pipeline_failure.md`.

## Mitigación (requiere aprobación humana)

Ninguna acción de mitigación se ejecuta de forma automática. Antes de cualquier paso:

1. Registrar en el ticket la evidencia de los puntos de diagnóstico anteriores.
2. Obtener aprobación explícita del DBA de guardia y del responsable del pipeline.
3. Pausar el DAG o job que origina la transacción abierta, para evitar que se reinicie el bloqueo.
4. Terminar únicamente la sesión bloqueante identificada en el paso 2, con el procedimiento aprobado por el DBA. No terminar sesiones de replicación ni procesos del sistema.
5. Confirmar que `lock_wait_seconds_max` vuelve por debajo del umbral y que la latencia p95 se normaliza durante 15 minutos.
6. Si el bloqueo se repite, revertir el despliegue del pipeline al release anterior, con aprobación del responsable de datos.

## Escalamiento

- Si no se libera el bloqueo en 15 minutos, escalar al DBA de guardia.
- Si hay impacto en clientes B2B, notificar al responsable de incidentes según la matriz de comunicación.

## Notas de seguridad

- Las consultas de diagnóstico son de solo lectura.
- Nunca ejecutar DROP, DELETE, TRUNCATE ni ALTER como parte de la respuesta a este incidente.
