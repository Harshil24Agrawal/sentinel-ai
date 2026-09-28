"""Detectors against the real generator scenarios (generator -> parser -> aggregator -> detectors).

Persistence, grouping and incidents are Phase 3: here we only check which signals appear, when.
"""

import copy

import pytest
from helpers import GenRig, Rig

from app.models import Severity, SignalType
from app.severity import compute_severity

ERR, LAT, SIL = SignalType.ERROR_RATE, SignalType.LATENCY, SignalType.SILENCE
NEW, SURGE = SignalType.NEW_PATTERN, SignalType.PATTERN_SURGE
WARMED = 180  # ticks of normal traffic before any fault (ready from ~90)

POOL = "Connection pool exhausted after <NUM> ms"
DB_TIMEOUT = "Database timeout while saving appointment <NUM>"
TOKEN = "Token validation failed: signing key <HEX> not found"


def templates(rig: Rig, service: str, kind: SignalType) -> set[str]:
    return {
        rig.agg.template(service, sig.pattern_id) or ""
        for _, sig in rig.signals(service, kind)
        if sig.pattern_id
    }


def scenario(warmed: GenRig, name: str, seconds: int) -> GenRig:
    rig = copy.deepcopy(warmed)
    rig.gen.start_scenario(name)
    rig.run(seconds)
    return rig


@pytest.fixture(scope="module")
def warmed() -> GenRig:
    rig = GenRig(seed=7)
    rig.run(WARMED)
    return rig


@pytest.fixture(scope="module")
def db_degradation(warmed: GenRig) -> GenRig:
    return scenario(warmed, "db_degradation", 75)


@pytest.fixture(scope="module")
def auth_outage(warmed: GenRig) -> GenRig:
    return scenario(warmed, "auth_outage", 60)


@pytest.fixture(scope="module")
def silent_service(warmed: GenRig) -> GenRig:
    return scenario(warmed, "silent_service", 75)


def test_normal_traffic_raises_no_signals_anywhere(warmed: GenRig) -> None:
    services = warmed.agg.services()
    assert len(services) == 7
    assert all(warmed.history[-1][s].ready for s in services)
    assert sum(r["database"].ready for r in warmed.history) >= 80  # a long ready stretch
    assert [(s, warmed.signals(s)) for s in services if warmed.signals(s)] == []


def test_phi_lines_do_not_disturb_detection(warmed: GenRig) -> None:
    assert warmed.parser.unparsed == 0
    assert warmed.parser._redactor.total > 0  # PHI was present in the traffic, and redacted


def test_db_degradation_is_seen_first_at_the_database(db_degradation: GenRig) -> None:
    rig = db_degradation
    assert rig.signals("database", since=0)[0][0] >= WARMED  # nothing before the fault

    latency = rig.first_signal_tick("database", LAT, since=WARMED)
    assert latency is not None and latency - WARMED <= 15  # x20 latency shows early
    assert templates(rig, "database", NEW) == {POOL}

    appointment = rig.first_signal_tick("appointment-service", ERR, since=WARMED)
    assert appointment is not None
    assert latency < appointment  # the origin deviates before its dependents
    assert appointment - WARMED < 45


def test_db_degradation_downstream_signals(db_degradation: GenRig) -> None:
    rig = db_degradation
    for service in ("appointment-service", "claims-service"):
        assert rig.signals(service, ERR, since=WARMED), service
        assert rig.signals(service, LAT, since=WARMED), service
        assert any("Database timeout while saving" in t for t in templates(rig, service, NEW))
    assert DB_TIMEOUT in templates(rig, "appointment-service", NEW)
    assert rig.signals("appointment-service", SURGE)
    # notification errors rise ~35s in, on a service that does not depend on the database
    assert rig.signals("notification-service", ERR, since=WARMED)
    # unrelated services stay clean
    for service in ("provider-service", "auth-service", "patient-portal"):
        assert rig.signals(service) == [], service


def test_db_degradation_severity_reaches_critical(db_degradation: GenRig) -> None:
    rig = db_degradation
    for service in ("database", "appointment-service"):
        signals = rig.history[-1][service].signals
        severity = compute_severity(
            signals,
            error_rate=rig.agg.window(service).error_rate,
            anomalous_services=3,
            settings=rig.settings,
        )
        assert severity == Severity.CRITICAL, service


def test_auth_outage_signals(auth_outage: GenRig) -> None:
    rig = auth_outage
    assert rig.signals("auth-service", ERR, since=WARMED)
    assert templates(rig, "auth-service", NEW) == {TOKEN}
    assert rig.signals("auth-service", SURGE)  # the ordinary 401/403 line surges too
    portal = rig.first_signal_tick("patient-portal", ERR, since=WARMED)
    auth = rig.first_signal_tick("auth-service", ERR, since=WARMED)
    assert portal is not None and auth is not None and auth < portal
    assert rig.signals("database") == []
    assert rig.signals("claims-service") == []


def test_silent_service_is_detected_as_silence(silent_service: GenRig) -> None:
    rig = silent_service
    first = rig.first_signal_tick("notification-service", SIL, since=WARMED)
    assert first is not None
    assert 40 <= first - WARMED <= 60  # whole-window lookback: ~48s until below 20% of normal
    assert rig.signals("notification-service", ERR) == []
    for service in rig.agg.services():
        if service != "notification-service":
            assert rig.signals(service) == [], service


def test_silence_is_critical_once_it_has_lasted_30_seconds(silent_service: GenRig) -> None:
    signals = silent_service.history[-1]["notification-service"].signals
    assert compute_severity(signals, silence_duration_s=29) is not None
    assert compute_severity(signals, silence_duration_s=30) == Severity.CRITICAL


def test_faster_silence_detection_when_configured(warmed: GenRig) -> None:
    fast = copy.deepcopy(warmed)
    fast.settings.SILENCE_LOOKBACK_SECONDS = 15  # detectors read settings on every call
    fast.gen.start_scenario("silent_service")
    fast.run(30)
    first = fast.first_signal_tick("notification-service", SIL, since=WARMED)
    assert first is not None and first - WARMED <= 15


def test_noisy_blip_is_absorbed_by_the_window_and_raises_no_signal(warmed: GenRig) -> None:
    """25% errors for 3s moves the 60s window by ~1 point: below the 2-point floor and below
    z=3 (so this is stronger than 'persistence filters it')."""
    rig = scenario(warmed, "noisy_blip", 100)
    assert rig.gen.scenario_started_at["noisy_blip"] == warmed.clock.now()
    assert rig.signals("claims-service", since=WARMED) == []
    peak = max(r["claims-service"].error_z or 0 for r in rig.history[WARMED:])
    assert peak < 3.0
    # (other services are not asserted: at these volumes ordinary sampling noise can graze the
    # threshold now and then; that is what persistence in the incident manager is for)


def test_recover_stops_new_faults_from_showing_up_in_the_window(db_degradation: GenRig) -> None:
    rig = copy.deepcopy(db_degradation)
    rig.gen.start_scenario("recover")
    rig.frozen.update(rig.agg.services())  # what open incidents will do: no learning
    rig.run(90)
    for service in ("database", "appointment-service", "claims-service", "notification-service"):
        last = rig.history[-1][service]
        assert not last.signals, service
        assert last.is_calm(1.5, 0.02), service
