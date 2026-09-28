"""End to end on a FakeClock: generator -> parser -> aggregator -> detectors -> incidents.

Realistic traffic volumes. A slow module: run everything with `pytest -m "slow or not slow"`.
The collector and the file round trip are covered in their own tests.
"""

from types import SimpleNamespace

import pytest
from helpers import WARMED, ManagedGenRig

from app.hints import DEFAULT_RULES, SILENCE_HINT
from app.models import IncidentState, Severity

DB, APPT, CLAIMS, NOTIF = (
    "database",
    "appointment-service",
    "claims-service",
    "notification-service",
)
AUTH, PORTAL, PROVIDER = "auth-service", "patient-portal", "provider-service"
pytestmark = pytest.mark.slow

DB_HINT, AUTH_HINT = DEFAULT_RULES[0][1], DEFAULT_RULES[1][1]


def test_healthy_system_has_no_incidents(warmed: ManagedGenRig) -> None:
    assert warmed.manager.incidents() == []
    assert warmed.manager.open_services() == set()
    assert warmed.notifies() == []
    assert warmed.events == []


# ---- db_degradation ---------------------------------------------------------------------


def test_db_degradation_is_exactly_one_correlation_group(db_outage: SimpleNamespace) -> None:
    manager = db_outage.rig.manager
    assert manager.groups_opened == 1
    incidents = manager.incidents()
    assert {i.service for i in incidents} >= {DB, APPT, CLAIMS}
    assert len({i.group_id for i in incidents}) == 1
    assert manager.group(incidents[0].group_id).is_open is False  # type: ignore[union-attr]


def test_db_degradation_origin_is_the_database(db_outage: SimpleNamespace) -> None:
    for incident in [*db_outage.mid, *db_outage.rig.manager.incidents()]:
        assert incident.origin == DB
        assert incident.blast_radius is not None and incident.blast_radius.origin == DB
    assert {i.service for i in db_outage.mid} >= {DB, APPT, CLAIMS}


def test_db_degradation_sends_at_most_three_sns_eligible_notifications(
    db_outage: SimpleNamespace,
) -> None:
    notes = db_outage.rig.notifies()
    assert 1 <= len(notes) <= 3
    assert notes[-1].kind == "resolved"
    announced = [n for n in notes if n.kind != "resolved"]
    assert announced and all(n.severity >= Severity.HIGH for n in announced)
    severities = [n.severity for n in announced]
    assert severities == sorted(set(severities))  # each severity announced once, escalating
    assert all(n.payload["origin"] == DB for n in notes)
    assert len(db_outage.mid_notifies) == len(
        announced
    )  # nothing more was sent for the outage itself


def test_db_degradation_is_detected_quickly_and_database_goes_first(
    db_outage: SimpleNamespace,
) -> None:
    incidents = sorted(db_outage.rig.manager.incidents(), key=lambda i: i.opened_at)
    assert incidents[0].service == DB
    injected = db_outage.rig.gen.fault_injected_at[DB]
    assert 0 < incidents[0].opened_at - injected <= 20
    opened = {i.service: i.opened_at for i in incidents}
    assert opened[DB] < opened[APPT] and opened[DB] < opened[CLAIMS]


def test_db_degradation_card_explains_what_where_and_why(db_outage: SimpleNamespace) -> None:
    by_service = {i.service: i for i in db_outage.mid}
    database = by_service[DB]
    assert database.hint == DB_HINT
    assert database.has_new_pattern
    pool = [p for p in database.patterns if p.template.startswith("Connection pool exhausted")]
    assert pool and pool[0].is_new and pool[0].service == DB
    assert len(database.patterns) <= 3
    assert any(sig.type.value == "latency" for sig in database.signals)
    assert database.failed_ops > 0 and database.peak_z >= 3
    assert 1 <= len(database.samples) <= 3
    assert database.max_severity == Severity.CRITICAL
    assert [e.to_severity for e in database.escalations][-1] == Severity.CRITICAL
    assert set(database.affected_services) >= {DB, APPT, CLAIMS}

    radius = {n.service: n.status for n in database.blast_radius.nodes}  # type: ignore[union-attr]
    assert radius[DB] == radius[APPT] == radius[CLAIMS] == "impacted"
    assert radius[AUTH] == radius[PORTAL] == radius[PROVIDER] == "at_risk"

    story = database.story
    assert [e.t for e in story] == sorted(e.t for e in story)
    assert {e.service for e in story} >= {DB, APPT, CLAIMS}


def test_db_degradation_card_keeps_its_evidence_after_recovery(db_outage: SimpleNamespace) -> None:
    for incident in db_outage.rig.manager.incidents():
        assert incident.state is IncidentState.RESOLVED
        assert incident.hint == DB_HINT  # not blanked as the window drains
        assert incident.patterns and incident.patterns[0].is_new


def test_db_degradation_incidents_hold_until_the_window_drains(db_outage: SimpleNamespace) -> None:
    incidents = db_outage.rig.manager.incidents()
    first_resolved = min(i.resolved_at for i in incidents)  # type: ignore[type-var]
    assert (
        first_resolved - db_outage.recovered_at >= 60
    )  # the 60s window still remembers the outage
    assert db_outage.resolve_ticks < 240
    assert db_outage.rig.manager.open_services() == set()


def test_baseline_stops_learning_while_the_outage_is_open(db_outage: SimpleNamespace) -> None:
    first_sample_tick = 59  # samples start once the 60s window is full
    unfrozen = WARMED + 75 - first_sample_tick  # what it would hold had it kept learning
    assert db_outage.baseline_samples[DB] < unfrozen - 30
    # it stopped when its incident opened (~14s in), so it gained roughly that many samples
    assert db_outage.baseline_samples[DB] <= WARMED - first_sample_tick + 25


def test_naive_detector_alerts_far_more_than_sentinel_notifies(db_outage: SimpleNamespace) -> None:
    assert db_outage.rig.naive.alerts >= 20
    assert db_outage.rig.naive.alerts > 5 * len(db_outage.rig.notifies())
    assert db_outage.rig.naive.alerts > 10 * db_outage.rig.manager.groups_opened


# ---- auth_outage ------------------------------------------------------------------------


def test_auth_outage_is_one_group_with_credentials_hint(auth_outage: SimpleNamespace) -> None:
    rig = auth_outage.rig
    assert rig.manager.groups_opened == 1
    assert {i.service for i in rig.manager.incidents()} == {AUTH, PORTAL}
    for incident in rig.manager.incidents():
        assert incident.origin == AUTH
        assert incident.hint == AUTH_HINT
        assert incident.patterns[0].template.startswith("Token validation failed")
        assert incident.patterns[0].is_new
    assert 1 <= len(rig.notifies()) <= 3
    assert {i.state for i in rig.manager.incidents()} == {IncidentState.RESOLVED}
    assert rig.notifies()[-1].kind == "resolved"


def test_auth_outage_leaves_unrelated_services_alone(auth_outage: SimpleNamespace) -> None:
    services = {i.service for i in auth_outage.rig.manager.incidents()}
    assert not services & {DB, CLAIMS, PROVIDER, APPT, NOTIF}


# ---- silent_service ---------------------------------------------------------------------


def test_silent_service_is_a_silence_incident(silent_outage: SimpleNamespace) -> None:
    rig = silent_outage.rig
    (incident,) = rig.manager.incidents()
    assert incident.service == NOTIF and incident.origin == NOTIF
    assert incident.state is IncidentState.RESOLVED
    assert incident.hint == SILENCE_HINT
    assert "service went silent" in [e.event for e in incident.timeline]
    assert incident.max_severity == Severity.CRITICAL  # silent for over 30s
    assert 0 < incident.opened_at - rig.gen.fault_injected_at[NOTIF] <= 30
    assert 1 <= len(rig.notifies()) <= 2
    assert rig.manager.groups_opened == 1


def test_silence_incident_resolves_once_the_service_is_logging_again(
    silent_outage: SimpleNamespace,
) -> None:
    (incident,) = silent_outage.rig.manager.incidents()
    assert incident.resolved_at is not None
    assert (
        incident.resolved_at - silent_outage.recovered_at >= 30
    )  # held for RESOLVE_TICKS at least
    assert silent_outage.resolve_ticks < 200


# ---- noisy_blip -------------------------------------------------------------------------


def test_noisy_blip_opens_no_incident(blip_run: ManagedGenRig) -> None:
    """Absorbed by the 60s window (25% for 3s moves it ~1 point, under the 2-point floor and
    under z=3), so not even a signal reaches the incident manager; persistence is not needed."""
    rig = blip_run
    assert rig.manager.incidents() == []
    assert rig.manager.groups_opened == 0
    assert rig.notifies() == []
    peak = max(r["claims-service"].error_z or 0 for r in rig.history[WARMED:])
    assert peak < 3.0
