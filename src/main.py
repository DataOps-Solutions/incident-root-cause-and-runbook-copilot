"""End-to-end entrypoint: ingest -> correlate -> retrieve runbooks -> RCA -> report.

Usage:
    LLM_PROVIDER=mock python -m src.main
"""

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from src.correlator import (
    MIN_RULES_FOR_RCA,
    CorrelationConfig,
    connect,
    ingest,
    load_changes,
    load_signals,
    run_correlation,
)
from src.models import CorrelatedIncident, Dataset, RCAReport, RCAReportBundle
from src.rca_agent import LLMProvider, RCAGenerationError, generate_rca, get_provider
from src.runbook_retriever import RunbookIndex, build_query

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "data" / "sample_signals.json"
DEFAULT_RUNBOOKS = ROOT / "data" / "runbooks"
DEFAULT_OUTPUT = ROOT / "output"
DEFAULT_DB_NAME = "incidents.db"
TOP_K_RUNBOOK_SECTIONS = 3


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _incident_query(incident: CorrelatedIncident, signal_messages: dict[str, str],
                    signal_names: dict[str, str], change_descriptions: dict[str, str]) -> str:
    parts: list[str] = list(incident.components)
    parts.extend(signal_names[s] for s in incident.signal_ids if s in signal_names)
    parts.extend(signal_messages[s] for s in incident.signal_ids if s in signal_messages)
    if incident.root_change_id and incident.root_change_id in change_descriptions:
        parts.append(change_descriptions[incident.root_change_id])
    return build_query(parts)


def run_pipeline(
    dataset_path: Path,
    runbooks_dir: Path,
    output_dir: Path,
    provider: LLMProvider,
    db_path: Path | None = None,
    config: CorrelationConfig | None = None,
    now: Callable[[], datetime] | None = None,
) -> RCAReportBundle:
    """Run the full pipeline and write `rca_report.json` and `rca_report.md`."""
    clock = now or _utcnow
    output_dir.mkdir(parents=True, exist_ok=True)
    database = db_path or output_dir / DEFAULT_DB_NAME
    if database.exists():
        database.unlink()  # fresh database per run keeps output reproducible

    dataset = Dataset.model_validate_json(dataset_path.read_text(encoding="utf-8"))
    conn = connect(database)
    try:
        counts = ingest(conn, dataset)
        incidents = run_correlation(conn, config)
        signals = load_signals(conn)
        changes = load_changes(conn)
        index = RunbookIndex.from_directory(runbooks_dir)

        signal_messages = {s.signal_id: s.message for s in signals}
        signal_names = {s.signal_id: s.name for s in signals}
        change_descriptions = {c.change_id: c.description for c in changes}

        reports: list[RCAReport] = []
        skipped: list[str] = []
        for incident in incidents:
            if len(incident.rules_fired) < MIN_RULES_FOR_RCA:
                skipped.append(incident.incident_id)
                continue
            query = _incident_query(incident, signal_messages, signal_names, change_descriptions)
            citations = index.search(query, top_k=TOP_K_RUNBOOK_SECTIONS)
            report = generate_rca(incident, signals, changes, citations, provider, clock)
            reports.append(report)
            conn.execute(
                "INSERT OR REPLACE INTO rca_reports (incident_id, llm_provider, model_id, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    report.incident_id,
                    report.llm_provider,
                    report.model_id,
                    report.model_dump_json(),
                    report.generated_at.isoformat(),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    bundle = RCAReportBundle(generated_at=clock(), reports=reports, skipped_incident_ids=skipped)
    (output_dir / "rca_report.json").write_text(
        bundle.model_dump_json(indent=2), encoding="utf-8"
    )
    (output_dir / "rca_report.md").write_text(render_markdown(bundle, counts, incidents), encoding="utf-8")
    return bundle


def render_markdown(
    bundle: RCAReportBundle,
    counts: dict[str, int],
    incidents: list[CorrelatedIncident],
) -> str:
    lines: list[str] = [
        "# Reporte de causa raiz (RCA)",
        "",
        f"Generado: {bundle.generated_at:%Y-%m-%d %H:%M} UTC",
        "",
        "## Resumen de ingesta",
        "",
        f"- Senales ingeridas: {counts['signals']}",
        f"- Cambios recientes: {counts['change_events']}",
        f"- Aristas de linaje: {counts['lineage_edges']}",
        f"- Incidentes correlacionados: {len(incidents)}",
        f"- Incidentes con RCA generado: {len(bundle.reports)}",
        "",
    ]
    if bundle.skipped_incident_ids:
        lines.append(
            "Incidentes omitidos por evidencia insuficiente: "
            + ", ".join(bundle.skipped_incident_ids)
        )
        lines.append("")

    for report in bundle.reports:
        lines.extend(_render_report(report))
    return "\n".join(lines).rstrip() + "\n"


def _render_report(report: RCAReport) -> list[str]:
    lines = [
        f"## Incidente {report.incident_id}",
        "",
        f"> {report.disclaimer}",
        "",
        f"Proveedor LLM: `{report.llm_provider}` (modelo `{report.model_id}`)",
        "",
        "### Causa raiz probable",
        "",
        report.probable_root_cause,
        "",
        f"Confianza: {report.confidence:.2f}",
        "",
        "### Evidencia",
        "",
    ]
    lines.extend(f"- {item}" for item in report.evidence)
    lines.extend(["", "### Factores contribuyentes", ""])
    lines.extend(f"- {item}" for item in report.contributing_factors or ["Sin factores adicionales."])
    lines.extend(["", "### Verificaciones de solo lectura", ""])
    if report.read_only_checks:
        for check in report.read_only_checks:
            lines.extend([f"- {check.description}:", "", "  ```sql", f"  {check.statement}", "  ```", ""])
    else:
        lines.extend(["Sin verificaciones tras la validacion de guardrails.", ""])
    lines.extend(["### Mitigaciones propuestas (requieren aprobacion humana)", ""])
    if report.mitigation_proposals:
        for proposal in report.mitigation_proposals:
            lines.append(
                f"- [riesgo {proposal.risk}] {proposal.description} Motivo: {proposal.rationale}"
            )
    else:
        lines.append("Sin mitigaciones propuestas.")
    lines.extend(["", "### Citas de runbooks", ""])
    for citation in report.runbook_citations:
        lines.extend(
            [
                f"- `{citation.file}` - seccion \"{citation.section}\" (score {citation.score:.2f}):",
                "",
                "  > " + citation.quote.replace("\n", "\n  > "),
                "",
            ]
        )
    if report.guardrail_notes:
        lines.extend(["### Notas de guardrails", ""])
        lines.extend(f"- {note}" for note in report.guardrail_notes)
        lines.append("")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Incident RCA copilot (end-to-end run).")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--runbooks", type=Path, default=DEFAULT_RUNBOOKS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--provider", choices=["mock", "bedrock"], default=None,
                        help="Overrides LLM_PROVIDER (default: mock).")
    parser.add_argument("--window-minutes", type=int, default=30)
    parser.add_argument("--lookback-minutes", type=int, default=120)
    args = parser.parse_args(argv)

    try:
        provider = get_provider(args.provider)
        bundle = run_pipeline(
            dataset_path=args.dataset,
            runbooks_dir=args.runbooks,
            output_dir=args.output,
            provider=provider,
            config=CorrelationConfig(
                window_minutes=args.window_minutes, lookback_minutes=args.lookback_minutes
            ),
        )
    except (RCAGenerationError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "provider": provider.name,
        "reports": len(bundle.reports),
        "skipped": bundle.skipped_incident_ids,
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
