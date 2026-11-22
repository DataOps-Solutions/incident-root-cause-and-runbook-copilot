"""Ingestion and deterministic temporal correlation of operational signals.

Correlation rules (all deterministic, no ML):
- A signal joins an open cluster when it occurs within `window_minutes` of the
  cluster's last signal AND its component is the same as, or lineage-linked to,
  a component already in the cluster.
- Each resulting cluster is scored by the rules it satisfies, plus the most
  recent change on the cluster's components or their upstreams within
  `lookback_minutes` before the first symptom.
"""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from src.models import (
    ChangeEvent,
    CorrelatedIncident,
    Dataset,
    LineageEdge,
    Severity,
    Signal,
    SignalType,
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"

DEFAULT_WINDOW_MINUTES = 30
DEFAULT_LOOKBACK_MINUTES = 120
RECENT_CHANGE_MINUTES = 60
MIN_RULES_FOR_RCA = 2

RULE_TEMPORAL = "temporal_cluster"
RULE_LINEAGE = "lineage_linked"
RULE_PRECURSOR = "precursor_degradation"
RULE_RECENT_CHANGE = "recent_change"
RULE_CRITICAL_ALERT = "critical_alert"

_SIGNAL_COLUMNS = (
    "signal_id, source, component, signal_type, name, severity, "
    "observed_at, value, threshold, message"
)
_CHANGE_COLUMNS = "change_id, component, change_type, description, actor, occurred_at, reference"


@dataclass(frozen=True)
class CorrelationConfig:
    """Tunable time windows for correlation."""

    window_minutes: int = DEFAULT_WINDOW_MINUTES
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES


class ComponentGraph:
    """Undirected adjacency plus directed upstream lookup over lineage edges."""

    def __init__(self, edges: Iterable[LineageEdge]) -> None:
        self._neighbors: dict[str, set[str]] = defaultdict(set)
        self._upstream: dict[str, set[str]] = defaultdict(set)
        for edge in edges:
            self._neighbors[edge.upstream].add(edge.downstream)
            self._neighbors[edge.downstream].add(edge.upstream)
            self._upstream[edge.downstream].add(edge.upstream)

    def related(self, first: str, second: str) -> bool:
        return first == second or second in self._neighbors.get(first, set())

    def linked(self, components: set[str], candidate: str) -> bool:
        return any(self.related(component, candidate) for component in components)

    def upstream_closure(self, components: set[str]) -> set[str]:
        """All transitive upstream components of the given set (excluding the set itself)."""
        seen: set[str] = set()
        frontier = list(components)
        while frontier:
            current = frontier.pop()
            for parent in self._upstream.get(current, set()):
                if parent not in seen:
                    seen.add(parent)
                    frontier.append(parent)
        return seen


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database and apply the schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def ingest(conn: sqlite3.Connection, dataset: Dataset) -> dict[str, int]:
    """Upsert signals, changes and lineage. Re-running is idempotent."""
    with conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO signals ({_SIGNAL_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    s.signal_id,
                    s.source,
                    s.component,
                    s.signal_type.value,
                    s.name,
                    s.severity.value,
                    s.observed_at.isoformat(),
                    s.value,
                    s.threshold,
                    s.message,
                )
                for s in dataset.signals
            ],
        )
        conn.executemany(
            f"INSERT OR REPLACE INTO change_events ({_CHANGE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    c.change_id,
                    c.component,
                    c.change_type.value,
                    c.description,
                    c.actor,
                    c.occurred_at.isoformat(),
                    c.reference,
                )
                for c in dataset.change_events
            ],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO component_lineage (upstream, downstream) VALUES (?, ?)",
            [(e.upstream, e.downstream) for e in dataset.lineage],
        )
    return {
        "signals": len(dataset.signals),
        "change_events": len(dataset.change_events),
        "lineage_edges": len(dataset.lineage),
    }


def load_signals(conn: sqlite3.Connection) -> list[Signal]:
    rows = conn.execute(f"SELECT {_SIGNAL_COLUMNS} FROM signals").fetchall()
    signals = [Signal.model_validate(dict(row)) for row in rows]
    return sorted(signals, key=lambda s: (s.observed_at, s.signal_id))


def load_changes(conn: sqlite3.Connection) -> list[ChangeEvent]:
    rows = conn.execute(f"SELECT {_CHANGE_COLUMNS} FROM change_events").fetchall()
    return [ChangeEvent.model_validate(dict(row)) for row in rows]


def load_lineage(conn: sqlite3.Connection) -> list[LineageEdge]:
    rows = conn.execute("SELECT upstream, downstream FROM component_lineage").fetchall()
    return [LineageEdge.model_validate(dict(row)) for row in rows]


def cluster_signals(
    signals: Iterable[Signal], graph: ComponentGraph, window_minutes: int
) -> list[list[Signal]]:
    """Group signals into clusters using the temporal + lineage join rule."""
    window = timedelta(minutes=window_minutes)
    clusters: list[list[Signal]] = []
    for signal in sorted(signals, key=lambda s: (s.observed_at, s.signal_id)):
        target: list[Signal] | None = None
        for cluster in clusters:
            last_seen = cluster[-1].observed_at
            components = {s.component for s in cluster}
            if signal.observed_at - last_seen <= window and graph.linked(
                components, signal.component
            ):
                target = cluster
                break
        if target is None:
            clusters.append([signal])
        else:
            target.append(signal)
    return clusters


def _evaluate_rules(
    cluster: list[Signal], components: list[str], root_change: ChangeEvent | None
) -> list[str]:
    rules: list[str] = []
    if len(cluster) >= 2:
        rules.append(RULE_TEMPORAL)
    if len(components) >= 2:
        rules.append(RULE_LINEAGE)

    alerts = [s for s in cluster if s.signal_type == SignalType.ALERT]
    if alerts:
        first_alert_at = min(s.observed_at for s in alerts)
        if any(
            s.signal_type != SignalType.ALERT
            and s.severity != Severity.INFO
            and s.observed_at < first_alert_at
            for s in cluster
        ):
            rules.append(RULE_PRECURSOR)
        if any(s.severity == Severity.CRITICAL for s in alerts):
            rules.append(RULE_CRITICAL_ALERT)

    if root_change is not None:
        rules.append(RULE_RECENT_CHANGE)
    return rules


def _build_incident(
    index: int,
    cluster: list[Signal],
    changes: list[ChangeEvent],
    graph: ComponentGraph,
    config: CorrelationConfig,
) -> CorrelatedIncident:
    start = cluster[0].observed_at
    end = cluster[-1].observed_at
    components = sorted({s.component for s in cluster})
    scope = graph.upstream_closure(set(components)) | set(components)

    lookback_start = start - timedelta(minutes=config.lookback_minutes)
    candidates = sorted(
        (c for c in changes if c.component in scope and lookback_start <= c.occurred_at <= start),
        key=lambda c: (c.occurred_at, c.change_id),
    )
    root_change = candidates[-1] if candidates else None
    root_component = root_change.component if root_change else cluster[0].component

    rules = _evaluate_rules(cluster, components, root_change)

    score = 0.15 * len(rules)
    if root_change is not None and start - root_change.occurred_at <= timedelta(
        minutes=RECENT_CHANGE_MINUTES
    ):
        score += 0.2
    score = round(min(1.0, score), 2)

    summary = (
        f"{len(cluster)} signals across {', '.join(components)} between "
        f"{start:%H:%M} and {end:%H:%M} UTC; root candidate {root_component}"
    )
    if root_change is not None:
        summary += f" after change {root_change.change_id} at {root_change.occurred_at:%H:%M} UTC"

    return CorrelatedIncident(
        incident_id=f"INC-{start:%Y%m%d%H%M}-{index:02d}",
        root_component=root_component,
        root_change_id=root_change.change_id if root_change else None,
        window_start=start,
        window_end=end,
        components=components,
        signal_ids=[s.signal_id for s in cluster],
        change_ids=[c.change_id for c in candidates],
        rules_fired=rules,
        score=score,
        summary=summary,
    )


def correlate(
    signals: list[Signal],
    changes: list[ChangeEvent],
    lineage: list[LineageEdge],
    config: CorrelationConfig | None = None,
) -> list[CorrelatedIncident]:
    """Pure correlation function: no I/O, deterministic for the same inputs."""
    cfg = config or CorrelationConfig()
    graph = ComponentGraph(lineage)
    clusters = cluster_signals(signals, graph, cfg.window_minutes)
    return [
        _build_incident(index, cluster, changes, graph, cfg)
        for index, cluster in enumerate(clusters, start=1)
    ]


def persist_incidents(conn: sqlite3.Connection, incidents: list[CorrelatedIncident]) -> None:
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO correlated_incidents
               (incident_id, root_component, root_change_id, window_start, window_end,
                components, signal_ids, change_ids, rules_fired, score, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    i.incident_id,
                    i.root_component,
                    i.root_change_id,
                    i.window_start.isoformat(),
                    i.window_end.isoformat(),
                    json.dumps(i.components),
                    json.dumps(i.signal_ids),
                    json.dumps(i.change_ids),
                    json.dumps(i.rules_fired),
                    i.score,
                    i.summary,
                )
                for i in incidents
            ],
        )


def run_correlation(
    conn: sqlite3.Connection, config: CorrelationConfig | None = None
) -> list[CorrelatedIncident]:
    """Read ingested data from the database, correlate it and persist the incidents."""
    incidents = correlate(
        load_signals(conn), load_changes(conn), load_lineage(conn), config
    )
    persist_incidents(conn, incidents)
    return incidents
