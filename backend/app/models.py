"""Pydantic models shared across the pipeline and API (CLAUDE.md §6-7)."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, PlainSerializer


class Level(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Severity(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


def _severity_from_name(value: Any) -> Any:
    return Severity[value.upper()] if isinstance(value, str) else value


# A Severity that travels as its name ("CRITICAL") in JSON and is parsed back from it, while
# staying an ordered IntEnum in Python (HIGH > MEDIUM works everywhere).
SeverityName = Annotated[
    Severity,
    BeforeValidator(_severity_from_name),
    PlainSerializer(lambda s: s.name, return_type=str, when_used="json"),
]


class SignalType(StrEnum):
    ERROR_RATE = "error_rate"
    LATENCY = "latency"
    SILENCE = "silence"
    NEW_PATTERN = "new_pattern"
    PATTERN_SURGE = "pattern_surge"


class IncidentState(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


ServiceHealth = Literal["healthy", "warming_up", "LOW", "MEDIUM", "HIGH", "CRITICAL", "silent"]
Channel = Literal["sns", "cloudwatch", "dryrun"]


class LogEvent(BaseModel):
    ts: float
    level: Level
    is_error: bool
    is_warn: bool
    service: str
    latency_ms: int | None = None
    message: str  # redacted
    raw: str  # redacted
    parsed: bool = True  # False: line matched no known format (service is "unknown")
    pattern_id: str | None = None  # set for error/warn lines only
    template: str | None = None


class LogLine(BaseModel):
    """One redacted log event as the live feed shows it (§7 ``logs`` message, ``/logs/recent``)."""

    ts: float
    level: Level
    service: str
    message: str  # redacted
    pattern_id: str | None = None  # error/warn lines only


class Signal(BaseModel):
    type: SignalType
    service: str
    value: float
    baseline_median: float
    z: float
    detail: str = ""
    pattern_id: str | None = None


class WindowStats(BaseModel):
    volume: int = 0
    errors: int = 0
    warns: int = 0
    error_rate: float = 0.0
    p95_latency: float | None = None
    latency_samples: int = 0
    pattern_counts: dict[str, int] = Field(default_factory=dict)
    distinct_error_patterns: int = 0


class BaselineView(BaseModel):
    error_rate_median: float = 0.0
    error_rate_band: tuple[float, float] = (0.0, 0.0)
    p95_median: float | None = None
    ready: bool = False


class PatternRef(BaseModel):
    pattern_id: str
    template: str
    count: int
    expected_per_min: float = 0.0
    is_new: bool = False
    surge_ratio: float | None = None
    service: str = ""


class TimelineEntry(BaseModel):
    t: float
    service: str
    event: str
    severity: SeverityName | None = None
    detail: str = ""


class Escalation(BaseModel):
    t: float
    from_severity: SeverityName | None = None
    to_severity: SeverityName


class BlastNode(BaseModel):
    service: str
    status: Literal["impacted", "at_risk", "healthy"]


class BlastRadius(BaseModel):
    origin: str
    nodes: list[BlastNode] = Field(default_factory=list)


class LLMExplanation(BaseModel):
    """Exact output schema the LLM must return (§6.13)."""

    is_incident: bool
    summary: str
    probable_cause: str
    affected_services: list[str] = Field(default_factory=list)
    recommended_checks: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]


class Explanation(LLMExplanation):
    source: Literal["rule_based", "llm"] = "rule_based"
    label: str = "Rule-based"  # UI shows "AI-generated — verify" for llm


class WhatIf(BaseModel):
    slope: float
    r_squared: float
    projected_error_rate: float
    horizon_seconds: int = 180
    at_risk_services: list[str] = Field(default_factory=list)
    label: str = "Projection if the trend continues — not a prediction."


class IncidentSummary(BaseModel):
    id: str
    service: str
    group_id: str
    state: IncidentState
    current_severity: SeverityName
    max_severity: SeverityName
    opened_at: float
    resolved_at: float | None = None
    title: str = ""
    has_new_pattern: bool = False
    origin: str | None = None
    affected_services: list[str] = Field(default_factory=list)


class Incident(IncidentSummary):
    """Full incident card (§6.10)."""

    what: str = ""  # metric now vs baseline
    signals: list[Signal] = Field(default_factory=list)
    peak_z: float = 0.0
    duration_s: float = 0.0
    failed_ops: int = 0
    escalations: list[Escalation] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    story: list[TimelineEntry] = Field(default_factory=list)  # merged across the group
    patterns: list[PatternRef] = Field(default_factory=list)
    hint: str = ""
    samples: list[str] = Field(default_factory=list)  # max 3, redacted
    blast_radius: BlastRadius | None = None
    ml_score: float | None = None  # stretch: per-service second opinion
    notified: bool = False
    anomaly_started_at: float | None = None  # first tick of the qualifying streak


class NotifyRequest(BaseModel):
    """A notification the incident manager wants sent (SNS-eligible: HIGH+ open/escalation,
    or the resolve of a group that notified). CloudWatch gets every IncidentEvent instead."""

    kind: Literal["opened", "escalated", "resolved"]
    incident_id: str  # the group's origin incident
    group_id: str
    severity: SeverityName
    subject: str
    body: str
    payload: dict[str, Any] = Field(default_factory=dict)


class IncidentEvent(BaseModel):
    """One state change of one incident, in the order it happened."""

    kind: Literal["opened", "updated", "resolved"]
    transition: str = ""  # "opened", "reopened", "severity_changed", "resolved", ...
    incident: Incident
    notify: NotifyRequest | None = None


class MemberIncident(BaseModel):
    """One service's incident, nested inside a group card."""

    id: str
    service: str
    state: IncidentState
    current_severity: SeverityName
    max_severity: SeverityName
    opened_at: float
    resolved_at: float | None = None
    duration_s: float = 0.0
    what: str = ""
    failed_ops: int = 0
    peak_z: float = 0.0
    has_new_pattern: bool = False
    signals: list[Signal] = Field(default_factory=list)
    escalations: list[Escalation] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    ml_score: float | None = None


class GroupSummary(BaseModel):
    """The unit the on-call engineer sees: one outage, however many services it touches."""

    id: str  # the correlation group id
    state: IncidentState
    current_severity: SeverityName
    max_severity: SeverityName
    opened_at: float
    resolved_at: float | None = None
    title: str = ""
    origin: str | None = None
    affected_services: list[str] = Field(default_factory=list)
    has_new_pattern: bool = False
    notified: bool = False
    incidents: list[MemberIncident] = Field(default_factory=list)


class GroupCard(GroupSummary):
    """The full card (§7): the summary plus the evidence and the explanation."""

    what: str = ""
    duration_s: float = 0.0
    failed_ops: int = 0
    peak_z: float = 0.0
    hint: str = ""
    patterns: list[PatternRef] = Field(default_factory=list)
    samples: list[str] = Field(default_factory=list)  # max 3, redacted
    story: list[TimelineEntry] = Field(default_factory=list)  # every member's timeline merged
    blast_radius: BlastRadius | None = None
    explanation: Explanation | None = None
    what_if: WhatIf | None = None  # stretch
    ml_score: float | None = None  # stretch


class NotificationEvent(BaseModel):
    channel: Channel
    incident_id: str
    severity: SeverityName
    ok: bool
    payload: dict[str, Any] = Field(default_factory=dict)


class ServiceBandView(BaseModel):
    volume: int
    error_rate: float
    p95_latency: float | None = None
    band: tuple[float, float] = (0.0, 0.0)
    status: ServiceHealth = "warming_up"


class ServiceStatus(BaseModel):
    name: str
    status: ServiceHealth
    volume: int
    error_rate: float
    p95_latency: float | None = None
    baseline: BaselineView = Field(default_factory=BaselineView)
    depends_on: list[str] = Field(default_factory=list)


class DeliveryLogEntry(BaseModel):
    """One outgoing send, for the dashboard's delivery log (last 10)."""

    ts: float
    channel: Channel
    severity: str | None = None
    ok: bool
    latency_ms: float


class Stats(BaseModel):
    naive_alerts: int = 0
    sentinel_incidents: int = 0
    sentinel_notifications: int = 0
    detection_latency_s: dict[str, float] = Field(default_factory=dict)
    phi_redacted: int = 0
    delivery_log: list[DeliveryLogEntry] = Field(default_factory=list)
