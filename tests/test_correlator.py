"""Tests for ingestion, temporal correlation rules and runbook retrieval citations."""

from datetime import timedelta
from pathlib import Path

from src.correlator import (
    RULE_LINEAGE,
    RULE_PRECURSOR,
    RULE_RECENT_CHANGE,
    RULE_TEMPORAL,
    CorrelationConfig,
    connect,
    correlate,
    ingest,
    load_changes,
    load_lineage,
    load_signals,
    run_correlation,
)
from src.models import Dataset
from src.runbook_retriever import RunbookIndex, tokenize
from tests.conftest import RUNBOOKS_DIR


def test_ingest_is_idempotent(tmp_path: Path, dataset: Dataset) -> None:
    conn = connect(tmp_path / "idem.db")
    first = ingest(conn, dataset)
    second = ingest(conn, dataset)
    assert first == second == {"signals": 11, "change_events": 2, "lineage_edges": 1}
    assert len(load_signals(conn)) == 11
    assert len(load_changes(conn)) == 2
    assert len(load_lineage(conn)) == 1
    conn.close()


def test_sample_produces_two_incidents_and_isolates_unrelated_signal(db) -> None:
    incidents = run_correlation(db)
    assert len(incidents) == 2
    main_incident = max(incidents, key=lambda i: len(i.signal_ids))
    assert "sig-011" not in main_incident.signal_ids
    unrelated = [i for i in incidents if i is not main_incident]
    assert unrelated[0].signal_ids == ["sig-011"]


def test_root_candidate_is_recent_etl_deploy(db) -> None:
    incidents = run_correlation(db)
    main_incident = max(incidents, key=lambda i: len(i.signal_ids))
    assert main_incident.root_change_id == "chg-001"
    assert main_incident.root_component == "etl-orders-daily"
    assert "chg-002" in main_incident.change_ids  # in scope but older, not selected as root


def test_expected_rules_fire_for_main_incident(db) -> None:
    main_incident = max(run_correlation(db), key=lambda i: len(i.signal_ids))
    assert {RULE_TEMPORAL, RULE_LINEAGE, RULE_PRECURSOR, RULE_RECENT_CHANGE} <= set(
        main_incident.rules_fired
    )
    assert 0.0 <= main_incident.score <= 1.0


def test_correlation_is_deterministic(db) -> None:
    first = correlate(load_signals(db), load_changes(db), load_lineage(db))
    second = correlate(load_signals(db), load_changes(db), load_lineage(db))
    assert first == second


def test_change_outside_lookback_is_not_selected(dataset: Dataset) -> None:
    signals = dataset.signals
    changes = [c for c in dataset.change_events if c.change_id == "chg-001"]
    shifted = [c.model_copy(update={"occurred_at": c.occurred_at - timedelta(hours=6)}) for c in changes]
    incidents = correlate(signals, shifted, dataset.lineage, CorrelationConfig(lookback_minutes=120))
    main_incident = max(incidents, key=lambda i: len(i.signal_ids))
    assert main_incident.root_change_id is None
    assert RULE_RECENT_CHANGE not in main_incident.rules_fired
    assert main_incident.root_component == "etl-orders-daily"  # earliest signal fallback


def test_window_split_separates_distant_linked_signals(dataset: Dataset) -> None:
    incidents = correlate(dataset.signals, dataset.change_events, dataset.lineage,
                          CorrelationConfig(window_minutes=5))
    assert max(len(i.signal_ids) for i in incidents) < 10


def test_incident_persisted_to_database(db) -> None:
    run_correlation(db)
    count = db.execute("SELECT COUNT(*) FROM correlated_incidents").fetchone()[0]
    assert count == 2


def test_runbook_citations_are_exact_substrings_of_source_files(runbook_index: RunbookIndex) -> None:
    citations = runbook_index.search("lock bloqueo latencia pg_blocking_pids", top_k=5)
    assert citations, "expected at least one citation"
    for citation in citations:
        source = RUNBOOKS_DIR / citation.file.split("/", 1)[1]
        content = source.read_text(encoding="utf-8")
        assert citation.quote in content
        assert f"## {citation.section}" in content


def test_runbook_ranking_prefers_relevant_runbook(runbook_index: RunbookIndex) -> None:
    top = runbook_index.search("pg_blocking_pids lock wait bloqueo", top_k=1)[0]
    assert top.file.endswith("db_high_latency_locks.md")
    schema = runbook_index.search("schema drift contract mismatch", top_k=1)[0]
    assert schema.file.endswith("etl_pipeline_failure.md")


def test_tokenizer_drops_stopwords_and_splits_camel_case() -> None:
    tokens = tokenize("The DBLocks and la latencia")
    assert "the" not in tokens and "and" not in tokens and "la" not in tokens
    assert "latencia" in tokens
