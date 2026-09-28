"""Detectors against the real generator scenarios (generator -> parser -> aggregator -> detectors).

The pipelines are shared with the incident scenario tests (see conftest.py). Here we only look
at which signals appeared, and when, up to the end of each outage.
"""

import copy
from types import SimpleNamespace

import pytest
from helpers import WARMED, ManagedGenRig, Rig

from app.models import Severity, SignalType
from app.severity import compute_severity

ERR, LAT, SIL = SignalType.ERROR_RATE, SignalType.LATENCY, SignalType.SILENCE
NEW, SURGE = SignalType.NEW_PATTERN, SignalType.PATTERN_SURGE

pytestmark = pytest.mark.slow

POOL = "Connection pool exhausted after <NUM> ms"
DB_TIMEOUT = "Database timeout while saving appointment <NUM>"
TOKEN = "Token validation failed: signing key <HEX> not found"


def during(run: SimpleNamespace, service: str, kind: SignalType | None = None) -> list:
    """Signals for a service from the fault start to the end of the outage (not the recovery)."""
    return [
        (tick, sig)
        for tick, sig in run.rig.signals(service, kind, since=WARMED)
        if tick <= run.mid_tick
    ]


def templates(run: SimpleNamespace, service: str, kind: SignalType) -> set[str]:
    return {
        run.rig.agg.template(service, sig.pattern_id) or ""
        for _, sig in during(run, service, kind)
        if sig.pattern_id
    }


def first_tick(run: SimpleNamespace, service: str, kind: SignalType) -> int | None:
    found = during(run, service, kind)
    return found[0][0] if found else None


def worst_severity(run: SimpleNamespace, service: str) -> Severity | None:
    signals = run.rig.history[run.mid_tick][service].signals
    error_rate = max((s.value for s in signals if s.type is ERR), default=0.0)
    return compute_severity(
        signals, error_rate=error_rate, anomalous_services=3, settings=run.rig.settings
    )


def test_normal_traffic_raises_no_signals_anywhere(warmed: ManagedGenRig) -> None:
    services = warmed.agg.services()
    assert len(services) == 7
    assert all(warmed.history[-1][s].ready for s in services)
    assert sum(r["database"].ready for r in warmed.history) >= 50  # a real ready stretch
    assert [(s, warmed.signals(s)) for s in services if warmed.signals(s)] == []


def test_phi_lines_do_not_disturb_detection(warmed: ManagedGenRig) -> None:
    assert warmed.parser.unparsed == 0
    assert warmed.parser._redactor.total > 0  # PHI was present in the traffic, and redacted


def test_db_degradation_is_seen_first_at_the_database(db_outage: SimpleNamespace) -> None:
    rig = db_outage.rig
    assert rig.signals("database")[0][0] >= WARMED  # nothing before the fault

    latency = first_tick(db_outage, "database", LAT)
    assert latency is not None and latency - WARMED <= 15  # x20 latency shows early
    assert templates(db_outage, "database", NEW) == {POOL}

    appointment = first_tick(db_outage, "appointment-service", ERR)
    assert appointment is not None
    assert latency < appointment  # the origin deviates before its dependents
    assert appointment - WARMED < 45


def test_db_degradation_downstream_signals(db_outage: SimpleNamespace) -> None:
    for service in ("appointment-service", "claims-service"):
        assert during(db_outage, service, ERR), service
        assert during(db_outage, service, LAT), service
        assert any("Database timeout while saving" in t for t in templates(db_outage, service, NEW))
    assert DB_TIMEOUT in templates(db_outage, "appointment-service", NEW)
    assert during(db_outage, "appointment-service", SURGE)
    # notification errors rise ~35s in, on a service that does not depend on the database
    assert during(db_outage, "notification-service", ERR)
    for service in ("provider-service", "auth-service", "patient-portal"):
        assert during(db_outage, service) == [], service


def test_db_degradation_severity_reaches_critical(db_outage: SimpleNamespace) -> None:
    for service in ("database", "appointment-service"):
        assert worst_severity(db_outage, service) == Severity.CRITICAL, service


def test_auth_outage_signals(auth_outage: SimpleNamespace) -> None:
    assert during(auth_outage, "auth-service", ERR)
    assert templates(auth_outage, "auth-service", NEW) == {TOKEN}
    assert during(auth_outage, "auth-service", SURGE)  # the ordinary 401/403 line surges too
    portal = first_tick(auth_outage, "patient-portal", ERR)
    auth = first_tick(auth_outage, "auth-service", ERR)
    assert portal is not None and auth is not None and auth < portal
    assert during(auth_outage, "database") == []
    assert during(auth_outage, "claims-service") == []


def test_silent_service_is_detected_as_silence(silent_outage: SimpleNamespace) -> None:
    first = first_tick(silent_outage, "notification-service", SIL)
    assert first is not None
    assert 12 <= first - WARMED <= 22  # 20s lookback: ~16s until below 20% of normal
    assert during(silent_outage, "notification-service", ERR) == []
    for service in silent_outage.rig.agg.services():
        if service != "notification-service":
            assert during(silent_outage, service) == [], service


def test_silence_is_critical_once_it_has_lasted_30_seconds(
    silent_outage: SimpleNamespace,
) -> None:
    rig = silent_outage.rig
    signals = rig.history[silent_outage.mid_tick]["notification-service"].signals
    assert compute_severity(signals, silence_duration_s=29) is not None
    assert compute_severity(signals, silence_duration_s=30) == Severity.CRITICAL


def test_whole_window_silence_lookback_is_slower(warmed: ManagedGenRig) -> None:
    slow = copy.deepcopy(warmed)
    slow.settings.SILENCE_LOOKBACK_SECONDS = 60  # the spec's literal rule; read on every call
    slow.gen.start_scenario("silent_service")
    slow.run(70)
    first = slow.first_signal_tick("notification-service", SIL, since=WARMED)
    assert first is not None and 40 <= first - WARMED <= 60


def test_noisy_blip_is_absorbed_by_the_window_and_raises_no_signal(
    blip_run: ManagedGenRig,
) -> None:
    """25% errors for 3s moves the 60s window by ~1 point: below the 2-point floor and below
    z=3 (so this is stronger than 'persistence filters it')."""
    rig: Rig = blip_run
    assert rig.signals("claims-service", since=WARMED) == []
    peak = max(r["claims-service"].error_z or 0 for r in rig.history[WARMED:])
    assert peak < 3.0
    # (other services are not asserted: at these volumes ordinary sampling noise can graze the
    # threshold now and then; that is what persistence in the incident manager is for)
