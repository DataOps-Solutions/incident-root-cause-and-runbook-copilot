"""Pydantic v2 data contracts for signals, changes, incidents and RCA outputs."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

DISCLAIMER = (
    "Borrador generado automaticamente. Validar con el equipo de guardia antes de "
    "ejecutar cualquier verificacion o accion."
)


def _to_utc(value: datetime) -> datetime:
    """Reject naive timestamps and normalize aware ones to UTC."""
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


UtcDateTime = Annotated[datetime, AfterValidator(_to_utc)]


class SignalType(StrEnum):
    ALERT = "alert"
    METRIC = "metric"
    LOG_ERROR = "log_error"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ChangeType(StrEnum):
    DEPLOY = "deploy"
    CONFIG = "config"
    SCHEMA_MIGRATION = "schema_migration"
    INFRA = "infra"


class Signal(BaseModel):
    """A normalized failure signal: alert, metric degradation or error log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    component: str = Field(min_length=1)
    signal_type: SignalType
    name: str = Field(min_length=1)
    severity: Severity
    observed_at: UtcDateTime
    value: float | None = None
    threshold: float | None = None
    message: str = ""


class ChangeEvent(BaseModel):
    """A recent change (deploy, config, migration, infra) on a component."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    change_id: str = Field(min_length=1)
    component: str = Field(min_length=1)
    change_type: ChangeType
    description: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    occurred_at: UtcDateTime
    reference: str = ""


class LineageEdge(BaseModel):
    """Directed dependency: `upstream` produces data consumed by `downstream`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    upstream: str = Field(min_length=1)
    downstream: str = Field(min_length=1)


class Dataset(BaseModel):
    """Input bundle loaded from JSON before ingestion."""

    model_config = ConfigDict(extra="forbid")

    signals: list[Signal] = Field(default_factory=list)
    change_events: list[ChangeEvent] = Field(default_factory=list)
    lineage: list[LineageEdge] = Field(default_factory=list)


class RunbookCitation(BaseModel):
    """A verbatim excerpt from a runbook section, with its provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(min_length=1)
    section: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    score: float = Field(ge=0.0)


class CorrelatedIncident(BaseModel):
    """A group of time-correlated signals plus the candidate root change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str = Field(min_length=1)
    root_component: str = Field(min_length=1)
    root_change_id: str | None = None
    window_start: UtcDateTime
    window_end: UtcDateTime
    components: list[str]
    signal_ids: list[str]
    change_ids: list[str]
    rules_fired: list[str]
    score: float = Field(ge=0.0, le=1.0)
    summary: str


class RCAContext(BaseModel):
    """Everything the LLM is allowed to see for one incident."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident: CorrelatedIncident
    signals: list[Signal]
    changes: list[ChangeEvent]
    runbook_excerpts: list[RunbookCitation]


class SuggestedCheck(BaseModel):
    """A verification step. Must be read-only; enforced by guardrails."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=5)
    statement: str = Field(min_length=1)
    read_only: bool


class MitigationProposal(BaseModel):
    """A mitigation that is only ever proposed, never executed automatically."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=5)
    rationale: str = Field(min_length=5)
    risk: Literal["low", "medium", "high"]
    requires_human_approval: Literal[True] = True


class RCAModelOutput(BaseModel):
    """Schema the LLM must return. Validated before anything is persisted."""

    model_config = ConfigDict(extra="forbid")

    probable_root_cause: str = Field(min_length=10)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(min_length=1)
    contributing_factors: list[str] = Field(default_factory=list)
    read_only_checks: list[SuggestedCheck] = Field(default_factory=list)
    mitigation_proposals: list[MitigationProposal] = Field(default_factory=list)


class RCAReport(BaseModel):
    """Final, audited RCA report for one incident."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    generated_at: UtcDateTime
    llm_provider: str
    model_id: str
    probable_root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str]
    contributing_factors: list[str] = Field(default_factory=list)
    read_only_checks: list[SuggestedCheck] = Field(default_factory=list)
    mitigation_proposals: list[MitigationProposal] = Field(default_factory=list)
    runbook_citations: list[RunbookCitation] = Field(default_factory=list)
    guardrail_notes: list[str] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


class RCAReportBundle(BaseModel):
    """Output file contents for one pipeline run."""

    model_config = ConfigDict(extra="forbid")

    generated_at: UtcDateTime
    reports: list[RCAReport]
    skipped_incident_ids: list[str] = Field(default_factory=list)
