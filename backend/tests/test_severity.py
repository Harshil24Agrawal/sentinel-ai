import pytest
from helpers import make_settings

from app.models import Severity, Signal, SignalType
from app.severity import compute_severity, severity_from_z

L, M, H, C = Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL


def sig(kind: SignalType, z: float = 0.0, service: str = "svc") -> Signal:
    return Signal(type=kind, service=service, value=1.0, baseline_median=0.0, z=z)


def err(z: float) -> Signal:
    return sig(SignalType.ERROR_RATE, z)


@pytest.mark.parametrize(
    ("z", "expected"),
    [(3.0, L), (4.99, L), (5.0, M), (7.99, M), (8.0, H), (11.99, H), (12.0, C), (50.0, C)],
)
def test_base_level_from_z(z: float, expected: Severity) -> None:
    assert severity_from_z(z) == expected
    assert compute_severity([err(z)]) == expected


def test_no_signals_no_severity() -> None:
    assert compute_severity([]) is None


def test_largest_statistical_z_wins() -> None:
    signals = [err(3.5), sig(SignalType.LATENCY, 9.0), sig(SignalType.SILENCE, 6.0)]
    assert compute_severity(signals) == H


def test_pattern_signals_alone_are_at_most_low() -> None:
    patterns = [sig(SignalType.NEW_PATTERN), sig(SignalType.PATTERN_SURGE)]
    assert compute_severity(patterns) == L
    # not even escalated by duration or correlation
    assert compute_severity(patterns, anomaly_duration_s=999, anomalous_services=5) == L


def test_pattern_signals_do_not_raise_a_statistical_severity() -> None:
    assert compute_severity([err(3.5), sig(SignalType.NEW_PATTERN)]) == L


def test_plus_one_when_the_anomaly_lasted_over_120_seconds() -> None:
    assert compute_severity([err(3.5)], anomaly_duration_s=120) == L  # "more than" 120
    assert compute_severity([err(3.5)], anomaly_duration_s=120.1) == M
    assert compute_severity([err(6.0)], anomaly_duration_s=300) == H


def test_critical_at_25_percent_error_rate() -> None:
    assert compute_severity([err(3.5)], error_rate=0.249) == L
    assert compute_severity([err(3.5)], error_rate=0.25) == C


def test_critical_when_silence_lasts_30_seconds() -> None:
    silence = [sig(SignalType.SILENCE, 4.0)]
    assert compute_severity(silence, silence_duration_s=29.9) == L
    assert compute_severity(silence, silence_duration_s=30) == C
    # the silence rule needs a silence signal
    assert compute_severity([err(3.5)], silence_duration_s=300) == L


def test_plus_one_for_a_correlated_group() -> None:
    assert compute_severity([err(3.5)], anomalous_services=1) == L
    assert compute_severity([err(3.5)], anomalous_services=2) == M
    assert compute_severity([err(9.0)], anomalous_services=3) == C


def test_modifiers_stack_and_cap_at_critical() -> None:
    assert compute_severity([err(3.5)], anomaly_duration_s=200, anomalous_services=2) == H
    assert compute_severity([err(11.0)], anomaly_duration_s=200, anomalous_services=4) == C
    assert compute_severity([err(50.0)], error_rate=0.9, anomalous_services=9) == C


def test_thresholds_are_configurable() -> None:
    s = make_settings(SEVERITY_Z_MEDIUM=4.0, SEVERITY_Z_HIGH=6.0, SEVERITY_Z_CRITICAL=8.0)
    assert severity_from_z(4.0, s) == M
    assert severity_from_z(6.0, s) == H
    assert compute_severity([err(8.0)], settings=s) == C
    s = make_settings(SEVERITY_LONG_ANOMALY_SECONDS=10, CRITICAL_ERROR_RATE=0.5)
    assert compute_severity([err(3.5)], anomaly_duration_s=11, settings=s) == M
    assert compute_severity([err(3.5)], error_rate=0.4, settings=s) == L
