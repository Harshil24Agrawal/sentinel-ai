"""Pydantic models shared across the pipeline and API (CLAUDE.md §6-7)."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


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


class TimelineEntry(BaseModel):
    t: float
    service: str
    event: str
    severity: Severity | None = None
    detail: str = ""


class Escalation(BaseModel):
    t: float
    from_severity: Severity | None = None
    to_severity: Severity


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
    current_severity: Severity
    max_severity: Severity
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
    explanation: Explanation | None = None
    what_if: WhatIf | None = None
    ml_score: float | None = None
    notified: bool = False


class NotificationEvent(BaseModel):
    channel: Channel
    incident_id: str
    severity: Severity
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


class Stats(BaseModel):
    naive_alerts: int = 0
    sentinel_incidents: int = 0
    sentinel_notifications: int = 0
    detection_latency_s: dict[str, float] = Field(default_factory=dict)
    phi_redacted: int = 0
