"""Severity levels from robust z-scores, adjusted for duration, impact and correlation."""

from __future__ import annotations

from collections.abc import Sequence

from app.config import Settings, get_settings
from app.models import Severity, Signal, SignalType

# Signals that carry a robust z. Pattern signals only add confidence (at most LOW on their own).
STATISTICAL_SIGNALS = frozenset({SignalType.ERROR_RATE, SignalType.LATENCY, SignalType.SILENCE})


def severity_from_z(z: float, settings: Settings | None = None) -> Severity:
    """Below 5 LOW, from 5 MEDIUM, from 8 HIGH, from 12 CRITICAL (a signal exists, so z has
    already cleared OPEN_Z)."""
    s = settings or get_settings()
    if z >= s.SEVERITY_Z_CRITICAL:
        return Severity.CRITICAL
    if z >= s.SEVERITY_Z_HIGH:
        return Severity.HIGH
    if z >= s.SEVERITY_Z_MEDIUM:
        return Severity.MEDIUM
    return Severity.LOW


def compute_severity(
    signals: Sequence[Signal],
    *,
    error_rate: float = 0.0,
    anomaly_duration_s: float = 0.0,
    silence_duration_s: float = 0.0,
    anomalous_services: int = 1,
    settings: Settings | None = None,
) -> Severity | None:
    """Severity of a service's active signals, or None if there are none.

    1. Base level from the largest z among the statistical signals.
    2. +1 level if the anomaly has lasted more than SEVERITY_LONG_ANOMALY_SECONDS.
    3. CRITICAL if the window error rate is at least CRITICAL_ERROR_RATE, or silence has
       lasted at least CRITICAL_SILENCE_SECONDS.
    4. +1 level if the correlation group has two or more anomalous services.
    Capped at CRITICAL. Pattern-only signals stay LOW: they explain, they don't escalate.
    """
    if not signals:
        return None
    s = settings or get_settings()
    statistical = [sig for sig in signals if sig.type in STATISTICAL_SIGNALS]
    if not statistical:
        return Severity.LOW

    level = int(severity_from_z(max(sig.z for sig in statistical), s))
    if anomaly_duration_s > s.SEVERITY_LONG_ANOMALY_SECONDS:
        level += 1
    silent = any(sig.type is SignalType.SILENCE for sig in statistical)
    if error_rate >= s.CRITICAL_ERROR_RATE or (
        silent and silence_duration_s >= s.CRITICAL_SILENCE_SECONDS
    ):
        level = int(Severity.CRITICAL)
    if anomalous_services >= 2:
        level += 1
    return Severity(min(level, int(Severity.CRITICAL)))
