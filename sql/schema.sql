-- Incident correlation schema.
-- Portable DDL: runs on SQLite (local, reproducible) and PostgreSQL 14+.
-- Timestamps are stored as UTC ISO-8601. List columns are TEXT (JSON arrays)
-- for portability; on PostgreSQL they can be migrated to JSONB without
-- changing the application contract.

CREATE TABLE IF NOT EXISTS signals (
    signal_id    TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    component    TEXT NOT NULL,
    signal_type  TEXT NOT NULL CHECK (signal_type IN ('alert', 'metric', 'log_error')),
    name         TEXT NOT NULL,
    severity     TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    observed_at  TIMESTAMPTZ NOT NULL,
    value        DOUBLE PRECISION,
    threshold    DOUBLE PRECISION,
    message      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS change_events (
    change_id    TEXT PRIMARY KEY,
    component    TEXT NOT NULL,
    change_type  TEXT NOT NULL CHECK (change_type IN ('deploy', 'config', 'schema_migration', 'infra')),
    description  TEXT NOT NULL,
    actor        TEXT NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL,
    reference    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS component_lineage (
    upstream     TEXT NOT NULL,
    downstream   TEXT NOT NULL,
    PRIMARY KEY (upstream, downstream)
);

CREATE TABLE IF NOT EXISTS correlated_incidents (
    incident_id     TEXT PRIMARY KEY,
    root_component  TEXT NOT NULL,
    root_change_id  TEXT,
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    components      TEXT NOT NULL,
    signal_ids      TEXT NOT NULL,
    change_ids      TEXT NOT NULL,
    rules_fired     TEXT NOT NULL,
    score           DOUBLE PRECISION NOT NULL CHECK (score >= 0 AND score <= 1),
    summary         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rca_reports (
    incident_id   TEXT PRIMARY KEY REFERENCES correlated_incidents (incident_id),
    llm_provider  TEXT NOT NULL,
    model_id      TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_component_time ON signals (component, observed_at);
CREATE INDEX IF NOT EXISTS idx_signals_observed_at ON signals (observed_at);
CREATE INDEX IF NOT EXISTS idx_changes_component_time ON change_events (component, occurred_at);
CREATE INDEX IF NOT EXISTS idx_incidents_root_window ON correlated_incidents (root_component, window_start);
