import random

import pytest
from helpers import Harness, evaluation, pattern_signal

from app.detectors import Evaluation
from app.incidents import IncidentManager, priority_key
from app.models import (
    Incident,
    IncidentState,
    IncidentSummary,
    Severity,
    Signal,
    SignalType,
)

LOW, MEDIUM, HIGH, CRITICAL = Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL
# error-rate z that lands in each severity band (no other modifiers in these tests)
Z_LOW, Z_MED, Z_HIGH, Z_CRIT = 4.0, 6.0, 9.0, 15.0


def kinds(events: list) -> list[str]:
    return [e.kind for e in events]


# ---- persistence ------------------------------------------------------------------------


def test_incident_opens_on_the_fifth_consecutive_signal_tick() -> None:
    h = Harness()
    for _ in range(4):
        assert h.tick({"a": Z_LOW}) == []
    assert h.manager.open_services() == set()
    events = h.tick({"a": Z_LOW})
    assert kinds(events) == ["opened"]
    assert events[0].transition == "opened"
    incident = events[0].incident
    assert (incident.service, incident.state) == ("a", IncidentState.OPEN)
    assert incident.current_severity == LOW
    assert h.manager.open_services() == {"a"}


def test_persistence_ticks_is_configurable() -> None:
    h = Harness(PERSIST_TICKS=2)
    assert h.tick({"a": Z_LOW}) == []
    assert kinds(h.tick({"a": Z_LOW})) == ["opened"]


def test_flickering_signal_never_opens_an_incident() -> None:
    """A signal that goes above and below OPEN_Z in runs of 1-4 ticks is persistence's job."""
    h = Harness()
    rng = random.Random(3)
    for _ in range(120):
        for _ in range(rng.randint(1, 4)):  # above OPEN_Z: the detector fires
            assert h.tick({"a": rng.uniform(3.2, 20.0)}) == []
        for _ in range(rng.randint(1, 3)):  # back below: the streak is broken
            assert h.tick({"a": rng.uniform(0.0, 2.9)}) == []
    assert h.manager.incidents() == []
    assert h.manager.open_services() == set()
    assert h.manager.groups_opened == 0
    assert h.notifies() == []


@pytest.mark.parametrize("run_length", [1, 2, 3, 4])
def test_every_run_shorter_than_persistence_is_ignored(run_length: int) -> None:
    h = Harness()
    for _ in range(50):
        h.run(run_length, {"a": 12.0})
        h.tick({"a": 0.5})
    assert h.manager.incidents() == []


def test_a_run_of_exactly_five_after_flickering_opens_one_incident() -> None:
    h = Harness()
    for _ in range(20):
        h.run(4, {"a": 8.0})
        h.tick({"a": 0.0})
    assert h.manager.incidents() == []
    events = h.run(5, {"a": 8.0})
    assert kinds(events) == ["opened"]
    assert len(h.manager.incidents()) == 1


def test_a_broken_streak_starts_counting_again() -> None:
    h = Harness()
    h.run(4, {"a": Z_LOW})
    h.tick({"a": 0.0})
    assert h.run(4, {"a": Z_LOW}) == []
    assert kinds(h.tick({"a": Z_LOW})) == ["opened"]


def test_services_are_counted_independently() -> None:
    h = Harness()
    h.run(3, {"a": Z_LOW, "b": 0.0})
    h.run(2, {"a": Z_LOW, "b": Z_LOW})
    assert h.manager.open_services() == {"a"}  # b has only 2 ticks


def test_unready_services_are_ignored() -> None:
    h = Harness()
    h.run(20, {"a": evaluation("a", 20.0, ready=False)})
    assert h.manager.incidents() == []


# ---- hysteresis: no flapping ------------------------------------------------------------


def test_z_hovering_between_resolve_and_open_does_not_flap() -> None:
    """After opening, z wandering between RESOLVE_Z (1.5) and OPEN_Z (3) neither resolves the
    incident (not calm) nor opens another or re-notifies (no signals)."""
    h = Harness()
    h.run(5, {"a": Z_HIGH})
    assert len(h.manager.incidents("open")) == 1
    events_before = len(h.events)
    rng = random.Random(4)
    for _ in range(300):
        assert h.tick({"a": rng.uniform(1.6, 2.9)}) == []
    assert len(h.events) == events_before
    (incident,) = h.manager.incidents()
    assert incident.state is IncidentState.OPEN and incident.resolved_at is None
    assert h.manager.groups_opened == 1
    assert len(h.notifies()) == 1  # just the original HIGH notification


def test_calm_must_be_unbroken_for_resolve_ticks() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    h.run(29, {"a": 0.0})
    h.tick({"a": 2.0})  # one tick above RESOLVE_Z resets the count
    h.run(29, {"a": 0.0})
    assert h.manager.incidents()[0].state is IncidentState.OPEN
    events = h.tick({"a": 0.0})  # the 30th consecutive calm tick
    assert kinds(events) == ["resolved"]
    assert h.manager.incidents()[0].state is IncidentState.RESOLVED


def test_resolves_at_exactly_resolve_ticks_and_stays_resolved_while_hovering() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    for _ in range(29):
        assert h.tick({"a": 0.0}) == []
    assert kinds(h.tick({"a": 0.0})) == ["resolved"]
    rng = random.Random(5)
    for _ in range(100):  # hovering afterwards: no signal, so nothing reopens
        assert h.tick({"a": rng.uniform(1.6, 2.9)}) == []
    assert h.manager.incidents()[0].state is IncidentState.RESOLVED
    assert h.manager.open_services() == set()


def test_flapping_around_the_open_threshold_after_opening_keeps_one_incident() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    before = len(h.events)
    for i in range(100):  # 3.1 (fires) / 2.9 (does not); stays under the 120s escalation
        assert h.tick({"a": 3.1 if i % 2 == 0 else 2.9}) == []
    assert len(h.events) == before
    assert len(h.manager.incidents()) == 1 and h.manager.groups_opened == 1


def test_resolve_bar_is_lower_than_open_bar() -> None:
    h = Harness(OPEN_Z=3.0, RESOLVE_Z=1.5)
    h.run(5, {"a": 5.0})
    h.run(100, {"a": 2.0})  # below OPEN_Z but above RESOLVE_Z
    assert h.manager.incidents()[0].state is IncidentState.OPEN
    h.run(30, {"a": 1.4})
    assert h.manager.incidents()[0].state is IncidentState.RESOLVED


def test_absolute_floor_also_blocks_resolution() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    # z is back under RESOLVE_Z, but the rate is still 5 points above normal
    still_high = Evaluation(
        "a", True, [], error_z=1.0, error_delta=0.05, latency_z=0.0, shortfall_z=0.0
    )
    h.run(60, {"a": still_high})
    assert h.manager.incidents()[0].state is IncidentState.OPEN


# ---- baseline freeze --------------------------------------------------------------------


def test_service_is_frozen_exactly_while_its_incident_is_open() -> None:
    h = Harness()
    assert h.manager.open_services() == set()
    h.run(5, {"a": Z_LOW})
    assert h.manager.open_services() == {"a"}
    h.run(29, {"a": 0.0})
    assert h.manager.open_services() == {"a"}
    h.tick({"a": 0.0})
    assert h.manager.open_services() == set()


# ---- one incident per service; grouping ------------------------------------------------


def test_one_open_incident_per_service_and_signals_update_it() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    first = h.incident("a")
    h.run(50, {"a": Z_MED})
    (only,) = h.manager.incidents()
    assert only.id == first.id
    assert only.current_severity == MEDIUM
    assert only.peak_z >= Z_MED


def test_two_services_get_two_incidents() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW, "b": Z_LOW})
    assert {i.service for i in h.manager.incidents()} == {"a", "b"}


# ---- severity and escalation ------------------------------------------------------------


def test_severity_follows_z_and_records_the_story() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    assert h.incident("a").current_severity == LOW
    h.tick({"a": Z_HIGH})
    assert h.incident("a").current_severity == HIGH
    h.tick({"a": Z_CRIT})
    assert h.incident("a").current_severity == CRITICAL
    h.tick({"a": Z_LOW})  # eases off
    inc = h.incident("a")
    assert inc.current_severity == LOW  # what it is now
    assert inc.max_severity == CRITICAL  # the worst it got
    assert [(e.from_severity, e.to_severity) for e in inc.escalations] == [
        (LOW, HIGH),
        (HIGH, CRITICAL),
    ]
    labels = [e.event for e in inc.timeline]
    assert labels[-3:] == ["escalated", "escalated", "de-escalated"]
    assert "opened" in labels


def test_current_severity_is_held_while_nothing_fires() -> None:
    h = Harness()
    h.run(5, {"a": Z_HIGH})
    h.run(10, {"a": 2.0})  # no signals: the card does not drop to "nothing"
    assert h.incident("a").current_severity == HIGH


def test_timeline_starts_when_each_signal_first_appeared() -> None:
    latency = Signal(
        type=SignalType.LATENCY,
        service="a",
        value=300.0,
        baseline_median=100.0,
        z=4.0,
        detail="p95 latency 300 ms vs normal 100 ms",
    )
    h = Harness()
    start = h.clock.now()
    h.run(2, {"a": evaluation("a", 0.0, extra=(latency,))})  # latency alone, ticks 0-1
    h.run(3, {"a": evaluation("a", Z_MED, extra=(latency,))})  # error rate joins at tick 2
    timeline = h.incident("a").timeline
    assert [e.event for e in timeline] == [
        "latency anomaly",
        "error rate anomaly",
        "opened",
    ]
    assert [e.t - start for e in timeline] == [0, 2, 4]  # backdated to first sighting
    assert timeline[0].detail.startswith("p95 latency")


def test_new_signal_types_during_an_incident_join_the_timeline() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    h.tick({"a": evaluation("a", Z_LOW, extra=(pattern_signal("a"),))})
    inc = h.incident("a")
    assert inc.timeline[-1].event == "new error pattern"
    assert inc.has_new_pattern is True


def test_pattern_only_signals_open_at_most_a_low_incident_without_notifying() -> None:
    h = Harness()
    quiet = evaluation("a", 0.0, extra=(pattern_signal("a"),))
    events = h.run(5, {"a": quiet})
    assert kinds(events) == ["opened"]
    inc = h.incident("a")
    assert inc.current_severity == LOW and inc.has_new_pattern
    h.run(200, {"a": quiet})
    assert h.incident("a").current_severity == LOW  # not escalated by time either
    assert h.notifies() == []


def test_surge_only_incident_is_not_marked_new() -> None:
    h = Harness()
    surge = evaluation("a", 0.0, extra=(pattern_signal("a", SignalType.PATTERN_SURGE),))
    h.run(5, {"a": surge})
    assert h.incident("a").has_new_pattern is False


def test_pattern_signals_keep_an_incident_open_until_they_stop() -> None:
    h = Harness()
    quiet = evaluation("a", 0.0, extra=(pattern_signal("a"),))
    h.run(5, {"a": quiet})
    h.run(100, {"a": quiet})
    assert h.incident("a").state is IncidentState.OPEN  # not calm while the pattern persists
    h.run(30, {"a": 0.0})
    assert h.incident("a").state is IncidentState.RESOLVED


def test_long_lasting_anomaly_is_escalated() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    assert h.incident("a").current_severity == LOW
    h.run(130, {"a": Z_LOW})  # past 120s since the anomaly began
    assert h.incident("a").current_severity == MEDIUM


# ---- notifications ----------------------------------------------------------------------


def test_low_and_medium_incidents_do_not_notify() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    h.run(10, {"a": Z_MED})
    assert h.notifies() == []
    h.run(30, {"a": 0.0})  # resolves: it never notified, so it stays quiet
    assert h.incident("a").state is IncidentState.RESOLVED
    assert h.notifies() == []


def test_open_at_high_notifies_once() -> None:
    h = Harness()
    events = h.run(5, {"a": Z_HIGH})
    (note,) = h.notifies()
    assert (note.kind, note.severity) == ("opened", HIGH)
    assert events[-1].notify is note
    h.run(100, {"a": Z_HIGH})
    assert len(h.notifies()) == 1  # never repeats for the same severity


def test_escalation_into_high_and_critical_each_notify_once() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    assert h.notifies() == []
    h.tick({"a": Z_HIGH})
    h.run(10, {"a": Z_HIGH})
    h.tick({"a": Z_CRIT})
    h.run(10, {"a": Z_CRIT})
    assert [(n.kind, n.severity) for n in h.notifies()] == [
        ("escalated", HIGH),
        ("escalated", CRITICAL),
    ]


def test_oscillating_severity_never_repeats_a_notification() -> None:
    h = Harness()
    h.run(5, {"a": Z_CRIT})
    for _ in range(20):
        h.run(3, {"a": Z_HIGH})
        h.run(3, {"a": Z_CRIT})
    assert len(h.notifies()) == 1  # CRITICAL was announced once


def test_resolve_notifies_only_if_the_incident_notified() -> None:
    h = Harness()
    h.run(5, {"a": Z_HIGH})
    h.run(30, {"a": 0.0})
    assert [(n.kind, n.severity) for n in h.notifies()] == [("opened", HIGH), ("resolved", HIGH)]


def test_notification_carries_a_compact_redacted_card() -> None:
    h = Harness()
    h.run(5, {"a": Z_HIGH})
    (note,) = h.notifies()
    assert note.subject == "[HIGH] a errors - 1 service affected"
    assert len(note.subject) <= 100 and note.subject.isascii()  # SNS subject limit
    assert "HIGH: a errors" not in note.body  # severity/title are the subject; not repeated
    assert note.body.startswith("Started ")
    assert "CHECK FIRST:" in note.body
    assert "WHAT'S WRONG:" in note.body
    assert "Error rate 15x normal (30% vs 2%)" in note.body
    assert "AFFECTED:" in note.body and "- a (origin)" in note.body
    assert "WHAT HAPPENED:" in note.body
    assert f"Ref: {h.incident('a').group_id}" in note.body
    assert note.payload["origin"] == "a" and note.payload["severity"] == "HIGH"
    assert note.incident_id == h.incident("a").id


def test_resolve_notification_is_a_short_summary_line() -> None:
    h = Harness()
    h.run(5, {"a": Z_HIGH})
    h.run(3, {"a": Z_CRIT})  # escalate to CRITICAL before recovering, for the "Peak:" line
    h.run(30, {"a": 0.0})
    notes = h.notifies()
    resolved = next(n for n in notes if n.kind == "resolved")
    assert resolved.subject == "[RESOLVED] a errors - 1 service affected"
    flat = resolved.body.replace("\n", " ")  # summary line is word-wrapped for a phone screen
    assert flat.startswith("RESOLVED: a errors. Lasted ")
    assert "Peak: CRITICAL. Affected: a. No action needed." in flat
    assert f"Ref: {h.incident('a').group_id}" in resolved.body


def test_full_lifecycle_notifies_at_most_three_times() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    h.run(3, {"a": Z_HIGH})
    h.run(3, {"a": Z_CRIT})
    h.run(30, {"a": 0.0})
    assert [n.kind for n in h.notifies()] == ["escalated", "escalated", "resolved"]


# ---- cooldown and reopen ----------------------------------------------------------------


def test_anomaly_within_cooldown_reopens_the_same_incident() -> None:
    h = Harness()
    h.run(5, {"a": Z_HIGH})
    first = h.incident("a")
    h.run(30, {"a": 0.0})
    assert h.incident("a").state is IncidentState.RESOLVED
    h.run(20, {"a": 0.0})  # 20s after resolving: inside the 60s cooldown
    events = h.run(5, {"a": Z_HIGH})

    assert [(e.kind, e.transition) for e in events] == [("opened", "reopened")]
    (inc,) = h.manager.incidents()
    assert inc.id == first.id
    assert inc.state is IncidentState.OPEN and inc.resolved_at is None
    assert [e.event for e in inc.timeline].count("reopened") == 1
    assert h.manager.groups_opened == 1
    assert h.manager.open_services() == {"a"}
    # the group was announced as resolved, so it is announced again
    assert [n.kind for n in h.notifies()] == ["opened", "resolved", "opened"]


def test_anomaly_after_cooldown_is_a_new_incident() -> None:
    h = Harness()
    h.run(5, {"a": Z_HIGH})
    first = h.incident("a")
    h.run(30, {"a": 0.0})
    h.run(70, {"a": 0.0})  # past the 60s cooldown
    h.run(5, {"a": Z_HIGH})
    ids = {i.id for i in h.manager.incidents()}
    assert len(ids) == 2 and first.id in ids
    assert h.manager.groups_opened == 2


def test_cooldown_boundary_is_inclusive() -> None:
    """Resolved at tick T; a 5-tick streak that follows k calm ticks opens at T + k + 5."""

    def incidents_after(idle_ticks: int) -> int:
        h = Harness(COOLDOWN_SECONDS=10)
        h.run(5, {"a": Z_LOW})
        h.run(30, {"a": 0.0})  # resolves on the last of these ticks
        h.run(idle_ticks, {"a": 0.0})
        h.run(5, {"a": Z_LOW})
        return len(h.manager.incidents())

    assert incidents_after(5) == 1  # exactly 10s after the resolve: still the same incident
    assert incidents_after(6) == 2  # 11s: a new one


# ---- events and store -------------------------------------------------------------------


def test_events_are_only_emitted_when_something_changed() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    for _ in range(60):
        assert h.tick({"a": Z_LOW}) == []
    events = h.tick({"a": Z_HIGH})
    assert [(e.kind, e.transition) for e in events] == [("updated", "severity_changed")]
    assert events[0].incident.current_severity == HIGH


def test_resolve_event_carries_the_final_state() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    events = h.run(30, {"a": 0.0})
    (event,) = events
    assert event.kind == "resolved"
    assert event.incident.state is IncidentState.RESOLVED
    assert event.incident.resolved_at is not None
    assert event.incident.timeline[-1].event == "resolved"


def test_events_hold_snapshots_not_live_objects() -> None:
    h = Harness()
    h.run(5, {"a": Z_LOW})
    snapshot = h.events[0][1].incident
    h.run(30, {"a": 0.0})
    assert snapshot.state is IncidentState.OPEN  # later changes do not rewrite history
    assert h.manager.get(snapshot.id).state is IncidentState.RESOLVED  # type: ignore[union-attr]


def test_every_state_change_is_written_to_the_store() -> None:
    h = Harness(store=True)
    assert h.store is not None
    h.run(5, {"a": Z_LOW})
    h.tick({"a": Z_HIGH})
    h.run(30, {"a": 0.0})
    incident = h.incident("a")
    assert [e["kind"] for e in h.store.events(incident.id)] == [
        "opened",
        "severity_changed",
        "resolved",
    ]
    stored = h.store.get_incident(incident.id)
    assert stored is not None
    assert stored.state is IncidentState.RESOLVED and stored.max_severity == HIGH
    assert h.store.list_incidents("open") == []
    assert [i.id for i in h.store.list_incidents("resolved")] == [incident.id]


def test_a_failing_store_never_stops_monitoring() -> None:
    h = Harness(store=True)
    assert h.store is not None
    h.store.close()  # every write from now on raises
    events = h.run(5, {"a": Z_LOW})
    assert kinds(events) == ["opened"]


# ---- read API ---------------------------------------------------------------------------


def make_summary(**kw: object) -> IncidentSummary:
    base: dict[str, object] = {
        "id": "i",
        "service": "s",
        "group_id": "g",
        "state": IncidentState.OPEN,
        "current_severity": MEDIUM,
        "max_severity": MEDIUM,
        "opened_at": 100.0,
    }
    return IncidentSummary(**{**base, **kw})  # type: ignore[arg-type]


def test_priority_order_open_then_severity_then_new_patterns_then_recency() -> None:
    incidents = [
        make_summary(id="resolved-critical", state=IncidentState.RESOLVED,
                     current_severity=CRITICAL, resolved_at=500.0),
        make_summary(id="surge-medium-newer", opened_at=300.0),
        make_summary(id="new-medium-older", has_new_pattern=True, opened_at=100.0),
        make_summary(id="surge-medium-older", opened_at=200.0),
        make_summary(id="high", current_severity=HIGH, opened_at=50.0),
        make_summary(id="low", current_severity=LOW, has_new_pattern=True, opened_at=400.0),
    ]  # fmt: skip
    ordered = [i.id for i in sorted(incidents, key=priority_key)]
    assert ordered == [
        "high",
        "new-medium-older",
        "surge-medium-newer",
        "surge-medium-older",
        "low",
        "resolved-critical",
    ]


def test_manager_lists_incidents_in_priority_order_and_filters_by_state() -> None:
    h = Harness()
    h.run(5, {"low": Z_LOW, "crit": Z_CRIT})
    h.run(30, {"low": 0.0, "crit": Z_CRIT})
    assert [i.service for i in h.manager.incidents("open")] == ["crit"]
    assert [i.service for i in h.manager.incidents("resolved")] == ["low"]
    assert [i.service for i in h.manager.incidents()] == ["crit", "low"]


def test_summary_projects_the_card() -> None:
    h = Harness()
    h.run(5, {"a": Z_HIGH})
    inc: Incident = h.incident("a")
    summary = IncidentManager.summary(inc)
    assert isinstance(summary, IncidentSummary) and not isinstance(summary, Incident)
    assert (summary.id, summary.service, summary.current_severity) == (inc.id, "a", HIGH)


def test_get_returns_none_for_unknown_ids() -> None:
    assert Harness().manager.get("nope") is None
