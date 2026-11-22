"""Guardrail and output-schema tests: destructive suggestions are blocked, schema is enforced."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.correlator import connect, ingest, run_correlation
from src.main import run_pipeline
from src.models import (
    Dataset,
    MitigationProposal,
    RCAContext,
    RCAModelOutput,
    RCAReport,
    SuggestedCheck,
)
from src.rca_agent import (
    BedrockProvider,
    MockProvider,
    RCAGenerationError,
    build_prompt,
    enforce_guardrails,
    get_provider,
    parse_model_output,
    validate_read_only_statement,
)
from tests.conftest import RUNBOOKS_DIR, SAMPLE_PATH

DESTRUCTIVE = [
    "DROP TABLE orders_fact;",
    "DELETE FROM orders_fact WHERE load_date < now();",
    "TRUNCATE orders_fact;",
    "ALTER TABLE orders_fact ADD COLUMN x int;",
    "UPDATE orders_fact SET status = 'x';",
    "INSERT INTO audit VALUES (1);",
    "GRANT ALL ON orders_fact TO public;",
    "SELECT 1; DROP TABLE orders_fact;",
    "SELECT * INTO backup_orders FROM orders_fact;",
    "WITH x AS (DELETE FROM orders_fact RETURNING *) SELECT * FROM x;",
    "SELECT pg_terminate_backend(1234);",
    "SELECT * FROM orders_fact FOR UPDATE;",
    "SELECT nextval('seq');",
]

READ_ONLY = [
    "SELECT pid, state FROM pg_stat_activity;",
    "WITH blocked AS (SELECT pid FROM pg_locks WHERE NOT granted) SELECT * FROM blocked;",
    "EXPLAIN SELECT 1;",
    "SHOW statement_timeout;",
]


@pytest.mark.parametrize("statement", DESTRUCTIVE)
def test_destructive_statements_are_blocked(statement: str) -> None:
    decision = validate_read_only_statement(statement)
    assert decision.allowed is False


@pytest.mark.parametrize("statement", READ_ONLY)
def test_read_only_statements_are_allowed(statement: str) -> None:
    assert validate_read_only_statement(statement).allowed is True


def test_enforce_guardrails_removes_unsafe_items_and_keeps_safe_ones() -> None:
    raw = RCAModelOutput(
        probable_root_cause="Causa raiz probable de prueba para validar filtros.",
        confidence=0.5,
        evidence=["evidencia de prueba"],
        read_only_checks=[
            SuggestedCheck(description="Chequeo seguro de sesiones", statement="SELECT 1;", read_only=True),
            SuggestedCheck(description="Chequeo inseguro de borrado", statement="DELETE FROM t;", read_only=True),
            SuggestedCheck(description="Marcado como escritura", statement="SELECT 1;", read_only=False),
        ],
        mitigation_proposals=[
            MitigationProposal(description="Pausar el DAG afectado de forma controlada",
                               rationale="Evitar reintentos del bloqueo.", risk="medium"),
            MitigationProposal(description="Ejecutar DROP TABLE staging.tmp para liberar espacio",
                               rationale="Liberar espacio rapidamente.", risk="high"),
        ],
    )
    safe, notes = enforce_guardrails(raw)
    assert [c.description for c in safe.read_only_checks] == ["Chequeo seguro de sesiones"]
    assert [m.risk for m in safe.mitigation_proposals] == ["medium"]
    assert len(notes) == 3
    assert all(m.requires_human_approval is True for m in safe.mitigation_proposals)


def test_mitigation_cannot_disable_human_approval() -> None:
    with pytest.raises(ValidationError):
        MitigationProposal.model_validate(
            {"description": "Terminar sesion bloqueante", "rationale": "Liberar lock",
             "risk": "high", "requires_human_approval": False}
        )


def test_output_schema_rejects_missing_or_out_of_range_fields() -> None:
    with pytest.raises(ValidationError):
        RCAModelOutput.model_validate({"probable_root_cause": "texto suficientemente largo", "confidence": 0.4})
    with pytest.raises(ValidationError):
        RCAModelOutput.model_validate(
            {"probable_root_cause": "texto suficientemente largo", "confidence": 1.7, "evidence": ["x"]}
        )
    with pytest.raises(ValidationError):
        RCAModelOutput.model_validate(
            {"probable_root_cause": "texto suficientemente largo", "confidence": 0.4,
             "evidence": ["x"], "unexpected": True}
        )


def test_parse_model_output_accepts_fenced_json_and_rejects_garbage() -> None:
    payload = {
        "probable_root_cause": "Causa raiz probable suficientemente descriptiva.",
        "confidence": 0.6,
        "evidence": ["senal 1"],
    }
    fenced = "Aqui va:\n```json\n" + json.dumps(payload) + "\n```"
    assert parse_model_output(fenced).confidence == 0.6
    with pytest.raises(RCAGenerationError):
        parse_model_output("no hay json aqui")
    with pytest.raises(RCAGenerationError):
        parse_model_output('{"probable_root_cause": "x"}')


def test_end_to_end_with_unsafe_mock_output_is_sanitized(tmp_path: Path) -> None:
    bundle = run_pipeline(
        dataset_path=SAMPLE_PATH,
        runbooks_dir=RUNBOOKS_DIR,
        output_dir=tmp_path,
        provider=MockProvider(inject_unsafe=True),
        now=lambda: datetime(2026, 11, 22, 13, 0, tzinfo=UTC),
    )
    assert bundle.reports, "expected at least one RCA report"
    report = bundle.reports[0]
    statements = " ".join(c.statement for c in report.read_only_checks).upper()
    assert "DELETE" not in statements
    assert all("DROP" not in m.description.upper() for m in report.mitigation_proposals)
    assert len(report.guardrail_notes) == 2

    reloaded = RCAReport.model_validate_json(json.dumps(json.loads((tmp_path / "rca_report.json").read_text())["reports"][0]))
    assert reloaded.incident_id == report.incident_id


def test_end_to_end_mock_writes_both_outputs(tmp_path: Path) -> None:
    run_pipeline(SAMPLE_PATH, RUNBOOKS_DIR, tmp_path, MockProvider())
    assert (tmp_path / "rca_report.json").exists()
    markdown = (tmp_path / "rca_report.md").read_text(encoding="utf-8")
    assert "Reporte de causa raiz" in markdown
    assert "Citas de runbooks" in markdown


def test_get_provider_selects_mock_and_rejects_unknown() -> None:
    assert get_provider(environ={"LLM_PROVIDER": "mock"}).name == "mock"
    with pytest.raises(RCAGenerationError):
        get_provider(environ={"LLM_PROVIDER": "openai"})


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeBedrockClient:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict[str, object]] = []

    def invoke_model(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        payload = {"content": [{"type": "text", "text": self._text}]}
        return {"body": _FakeBody(json.dumps(payload).encode("utf-8"))}


def test_bedrock_provider_parses_response_with_injected_client(tmp_path: Path) -> None:
    dataset = Dataset.model_validate_json(SAMPLE_PATH.read_text(encoding="utf-8"))
    conn = connect(tmp_path / "bedrock.db")
    ingest(conn, dataset)
    incident = run_correlation(conn)[0]
    conn.close()

    context = RCAContext(
        incident=incident,
        signals=dataset.signals,
        changes=dataset.change_events,
        runbook_excerpts=[],
    )
    bundle = build_prompt(context)
    expected = MockProvider().generate(bundle)

    client = _FakeBedrockClient(expected)
    provider = BedrockProvider(model_id="test-model", region="us-east-1", client=client)
    raw = provider.generate(bundle)

    assert client.calls[0]["modelId"] == "test-model"
    assert parse_model_output(raw) == parse_model_output(expected)
