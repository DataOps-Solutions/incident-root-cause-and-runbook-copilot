# Runbook: fallo de pipeline ETL por schema drift o falta de locks

Ámbito: DAGs de ingesta batch (Airflow) que cargan tablas de hechos en `orders-db`.
Severidad típica: SEV-3 si el SLA se incumple sin impacto en clientes; SEV-2 si hay datos faltantes en reportes B2B.

## Síntomas

- Un task falla con errores de tipo `schema drift`, `column not found` o `contract mismatch`.
- Un task reintenta varias veces con `OperationalError` relacionado con locks sobre una tabla de destino.
- La duración de un task supera ampliamente su duración habitual.
- Se incumple el SLA del DAG.

## Diagnóstico de solo lectura

1. Historial reciente de ejecuciones del DAG (base de metadatos de Airflow):

   ```sql
   SELECT dag_id, run_id, state, start_date, end_date
   FROM dag_run
   WHERE dag_id = 'etl_orders_daily'
   ORDER BY start_date DESC
   LIMIT 5;
   ```

2. Revisar el contrato de datos de la fuente y compararlo con la versión desplegada del esquema. Verificar si la columna reportada como faltante fue eliminada o renombrada en la fuente.

3. Revisar en CI/CD los despliegues de los últimos 60 minutos del pipeline y su diff de transformación.

4. Si el error es de lock, aplicar el diagnóstico del runbook `db_high_latency_locks.md` sobre la base de destino.

## Interpretación

- Schema drift: la fuente cambió su contrato y el pipeline no lo tolera. Es un fallo de ingesta, no de la base.
- Lock wait en la carga: una transacción de larga duración del propio pipeline bloquea la tabla de hechos. Revisar si el último deploy cambió el patrón transaccional.

## Mitigación (requiere aprobación humana)

Ninguna acción se ejecuta de forma automática. Requiere aprobación del responsable del pipeline:

1. Pausar el DAG para evitar reintentos que amplifiquen el bloqueo.
2. Si el deploy reciente introdujo el problema, revertir al release anterior del pipeline.
3. Para schema drift, coordinar con el equipo de la fuente la restauración del contrato o la actualización explícita del mapeo en un cambio revisado, nunca editando tablas productivas de forma directa.
4. Reejecutar el DAG solo para la ventana afectada, una vez validada la causa.

## Escalamiento

- Si el SLA de reportes B2B está en riesgo, notificar al responsable de datos y al cliente según el plan de comunicación.

## Notas de seguridad

- Las consultas de diagnóstico son de solo lectura.
- No modificar tablas de destino manualmente durante el incidente.
