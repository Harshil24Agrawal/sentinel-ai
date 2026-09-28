import re
from collections import defaultdict
from pathlib import Path

import pytest

from app.clock import FakeClock
from app.collector import LogCollector
from app.models import LogEvent
from app.parser import LogParser
from app.patterns import template_of
from app.redaction import Redactor
from generator.generator import LogGenerator
from generator.scenarios import SCENARIO_NAMES, UnknownScenarioError
from generator.services import load_services

PHI_KEYS = re.compile(r"member_id=|ssn=|email=|patient_name=")


@pytest.fixture
def gen(clock: FakeClock, tmp_path: Path) -> LogGenerator:
    return LogGenerator(clock, tmp_path / "logs" / "app.log", seed=7)


def simulate(gen: LogGenerator, clock: FakeClock, seconds: int) -> list[list[LogEvent]]:
    """Events per simulated second, parsed like the real pipeline would."""
    parser = LogParser(clock, redactor=Redactor())
    per_second = []
    for _ in range(seconds):
        per_second.append([parser.parse(line) for line in gen.generate()])
        clock.advance(1)
    assert parser.unparsed == 0
    return per_second


def slice_of(seconds: list[list[LogEvent]], start: int, end: int, service: str) -> list[LogEvent]:
    return [e for events in seconds[start:end] for e in events if e.service == service]


def error_rate(events: list[LogEvent]) -> float:
    return sum(e.is_error for e in events) / len(events)


def mean_latency(events: list[LogEvent]) -> float:
    values = [e.latency_ms for e in events if e.latency_ms is not None]
    return sum(values) / len(values)


def test_services_yaml_matches_spec() -> None:
    services = load_services()
    assert len(services) == 7
    assert services["database"].depends_on == ()
    assert services["appointment-service"].depends_on == ("database", "notification-service")
    assert services["claims-service"].rps == 25
    assert services["database"].latency_mean_ms == 8


@pytest.mark.slow
def test_normal_traffic_volume_and_error_rates(gen: LogGenerator, clock: FakeClock) -> None:
    seconds = simulate(gen, clock, 60)
    for name, spec in gen.services.items():
        events = slice_of(seconds, 0, 60, name)
        assert len(events) == pytest.approx(spec.rps * 60, rel=0.15), name
        assert error_rate(events) < spec.base_error + 0.03, name
    assert not gen.active_scenarios()


def test_lines_are_time_ordered_within_the_last_second(gen: LogGenerator, clock: FakeClock) -> None:
    now = clock.now()
    parser = LogParser(clock, redactor=Redactor())
    stamps = [parser.parse(line).ts for line in gen.generate()]
    assert stamps == sorted(stamps)
    assert now - 1.0 <= stamps[0] and stamps[-1] <= now


def test_tick_appends_to_the_log_file(gen: LogGenerator, tmp_path: Path, clock: FakeClock) -> None:
    n1 = gen.tick()
    clock.advance(1)
    n2 = gen.tick()
    lines = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == n1 + n2 > 0


def test_same_seed_gives_same_traffic(clock: FakeClock, tmp_path: Path) -> None:
    a = LogGenerator(FakeClock(5.0), tmp_path / "a.log", seed=3)
    b = LogGenerator(FakeClock(5.0), tmp_path / "b.log", seed=3)
    assert a.generate() == b.generate()


@pytest.mark.slow
def test_phi_like_fields_appear_about_once_per_200_normal_lines_and_get_redacted(
    gen: LogGenerator, clock: FakeClock
) -> None:
    lines = [line for _ in range(150) for line in gen.generate()]
    phi = [line for line in lines if PHI_KEYS.search(line)]
    assert 0.002 < len(phi) / len(lines) < 0.010
    assert all(" ERROR " not in line and " WARN " not in line for line in phi)
    redactor = Redactor()
    cleaned = [redactor.redact(line)[0] for line in phi]
    assert redactor.total >= len(phi)
    assert not any(re.search(r"MBR\d|\d{3}-\d{2}-\d{4}|@example\.com|Jane|Doe", c) for c in cleaned)


@pytest.mark.slow
def test_base_error_messages_use_a_single_stable_pattern_per_service(
    gen: LogGenerator, clock: FakeClock
) -> None:
    """Otherwise a rarely-seen normal error would look like a NEW pattern after warm-up."""
    seconds = simulate(gen, clock, 120)
    patterns: dict[tuple[str, bool], set[str]] = defaultdict(set)
    for events in seconds:
        for e in events:
            if e.template:
                patterns[(e.service, e.is_error)].add(e.template)
    assert patterns and all(len(p) == 1 for p in patterns.values()), patterns


@pytest.mark.slow
def test_db_degradation(gen: LogGenerator, clock: FakeClock) -> None:
    start = gen.start_scenario("db_degradation")
    seconds = simulate(gen, clock, 75)

    # Fault times are recorded per service, for detection-latency measurement.
    assert gen.fault_injected_at == {
        "database": start,
        "appointment-service": start + 15,
        "claims-service": start + 15,
        "notification-service": start + 35,
    }
    assert gen.scenario_started_at["db_degradation"] == start
    assert gen.scenario_origin["db_degradation"] == "database"

    # Database latency x20, and it is the origin: pool exhaustion is a NEW pattern after ~15s.
    db_early = slice_of(seconds, 0, 5, "database")
    db_late = slice_of(seconds, 20, 30, "database")
    assert mean_latency(db_late) > 10 * mean_latency(db_early)
    pool = "Connection pool exhausted after <NUM> ms"
    assert not any(e.template == pool for e in slice_of(seconds, 0, 15, "database"))
    assert any(e.template == pool for e in slice_of(seconds, 15, 60, "database"))
    assert all(
        template_of(e.message) == pool for e in slice_of(seconds, 45, 60, "database") if e.is_error
    )

    # Appointment/claims rise to ~30% errors with DB timeouts.
    for name in ("appointment-service", "claims-service"):
        assert error_rate(slice_of(seconds, 0, 12, name)) < 0.08
        late = slice_of(seconds, 40, 60, name)
        assert 0.22 < error_rate(late) < 0.38, name
        errors = [e for e in late if e.is_error]
        assert all(e.message.startswith("Database timeout while saving") for e in errors)
        timed = [e.latency_ms for e in errors if e.latency_ms is not None]  # ~10% omit latency
        assert sum(ms > 2000 for ms in timed) > 0.9 * len(timed)

    # Notification errors rise later (from ~35s).
    assert error_rate(slice_of(seconds, 0, 30, "notification-service")) < 0.06
    assert 0.10 < error_rate(slice_of(seconds, 55, 75, "notification-service")) < 0.32

    # Unrelated services are unaffected.
    assert error_rate(slice_of(seconds, 40, 60, "provider-service")) < 0.05


@pytest.mark.slow
def test_auth_outage(gen: LogGenerator, clock: FakeClock) -> None:
    start = gen.start_scenario("auth_outage")
    seconds = simulate(gen, clock, 45)
    assert gen.fault_injected_at == {"auth-service": start, "patient-portal": start + 15}

    auth = slice_of(seconds, 15, 45, "auth-service")
    assert 0.28 < error_rate(auth) < 0.52  # target 0.40
    templates = {e.template for e in auth if e.is_error}
    assert "Token validation failed: signing key <HEX> not found" in templates
    assert "Login rejected with HTTP <NUM> for request <NUM>" in templates  # still present: surges
    assert any(e.latency_ms is not None and e.latency_ms < 200 for e in auth)

    assert error_rate(slice_of(seconds, 0, 12, "patient-portal")) < 0.08
    assert 0.09 < error_rate(slice_of(seconds, 35, 45, "patient-portal")) < 0.22
    assert error_rate(slice_of(seconds, 20, 45, "database")) < 0.05


@pytest.mark.slow
def test_silent_service_stops_logging_entirely(gen: LogGenerator, clock: FakeClock) -> None:
    before = simulate(gen, clock, 5)
    assert slice_of(before, 0, 5, "notification-service")
    gen.start_scenario("silent_service")
    after = simulate(gen, clock, 20)
    assert not slice_of(after, 0, 20, "notification-service")
    assert slice_of(after, 0, 20, "database")  # everyone else keeps logging


@pytest.mark.slow
def test_noisy_blip_lasts_three_seconds_only(gen: LogGenerator, clock: FakeClock) -> None:
    gen.start_scenario("noisy_blip")
    seconds = simulate(gen, clock, 20)
    assert error_rate(slice_of(seconds, 0, 3, "claims-service")) > 0.12
    assert error_rate(slice_of(seconds, 3, 20, "claims-service")) < 0.06
    assert gen.active_scenarios() == []


def test_noisy_blip_can_target_any_service_and_rejects_unknown(gen: LogGenerator) -> None:
    gen.start_scenario("noisy_blip", service="database")
    assert gen.scenario_origin["noisy_blip"] == "database"
    with pytest.raises(ValueError):
        gen.start_scenario("noisy_blip", service="nope")


@pytest.mark.slow
def test_recover_returns_everything_to_normal(gen: LogGenerator, clock: FakeClock) -> None:
    gen.start_scenario("db_degradation")
    simulate(gen, clock, 45)
    assert gen.active_scenarios() == ["db_degradation"]
    gen.start_scenario("recover")
    assert gen.active_scenarios() == []
    seconds = simulate(gen, clock, 30)
    assert error_rate(slice_of(seconds, 0, 30, "appointment-service")) < 0.06
    assert error_rate(slice_of(seconds, 0, 30, "database")) < 0.03
    assert not any("timeout" in e.message.lower() for ev in seconds for e in ev if e.is_error)
    db = slice_of(seconds, 0, 30, "database")
    assert sum(e.latency_ms or 0 for e in db) / len(db) < 30


def test_unknown_scenario_is_rejected(gen: LogGenerator) -> None:
    with pytest.raises(UnknownScenarioError):
        gen.start_scenario("meteor_strike")


def test_scenario_names_match_the_spec() -> None:
    assert set(SCENARIO_NAMES) == {
        "db_degradation", "auth_outage", "silent_service", "noisy_blip", "recover",
    }  # fmt: skip


def test_generator_to_file_to_collector_to_parser(
    gen: LogGenerator, clock: FakeClock, tmp_path: Path
) -> None:
    """The generator only talks to the collector through the log file."""
    collector = LogCollector(tmp_path / "logs" / "app.log")
    assert collector.poll_once() == []  # file does not exist yet: waits
    gen.tick()  # creates the file
    collector.poll_once()  # first sight of an existing file starts at its end
    written = 0
    for _ in range(5):
        clock.advance(1)
        written += gen.tick()
    lines = collector.poll_once()
    assert len(lines) == written > 0
    parser = LogParser(clock, redactor=Redactor())
    assert all(parser.parse(line).parsed for line in lines)
