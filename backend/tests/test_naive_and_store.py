from pathlib import Path

import pytest
from helpers import Feed, ManagedRig

from app.models import (
    Incident,
    IncidentState,
    PatternRef,
    Severity,
    TimelineEntry,
)
from app.store import Store

# ---- naive detector ---------------------------------------------------------------------


def test_naive_alerts_every_five_seconds_for_each_service_over_five_percent() -> None:
    bad, ok = Feed("bad", lines=20, p_error=0.30), Feed("ok", lines=20, p_error=0.0)
    rig = ManagedRig(bad, ok)
    rig.run(60)
    assert rig.naive.alerts_by_service.get("ok", 0) == 0
    assert rig.naive.alerts_by_service["bad"] == 12  # one check per 5s over 60s
    assert rig.naive.alerts == 12


def test_naive_counts_each_service_separately_with_no_grouping() -> None:
    rig = ManagedRig(*(Feed(n, lines=20, p_error=0.30) for n in "abc"))
    rig.run(30)
    assert rig.naive.alerts == 3 * 6
    assert set(rig.naive.alerts_by_service) == {"a", "b", "c"}


def test_naive_never_deduplicates_or_backs_off() -> None:
    rig = ManagedRig(Feed("bad", lines=20, p_error=0.30))
    rig.run(30)
    first_half = rig.naive.alerts
    rig.run(30)
    assert rig.naive.alerts == 2 * first_half  # the same outage pages again and again


def test_naive_stays_quiet_below_the_threshold() -> None:
    rig = ManagedRig(Feed("svc", lines=40, p_error=0.02))
    rig.run(120)
    assert rig.naive.alerts == 0


def test_naive_interval_is_configurable() -> None:
    rig = ManagedRig(Feed("bad", lines=20, p_error=0.30), NAIVE_INTERVAL_SECONDS=10)
    rig.run(60)
    assert rig.naive.alerts == 6


# ---- store ------------------------------------------------------------------------------


def make_incident(id: str = "inc-1", **kw: object) -> Incident:
    base: dict[str, object] = {
        "id": id,
        "service": "database",
        "group_id": "grp-1",
        "state": IncidentState.OPEN,
        "current_severity": Severity.HIGH,
        "max_severity": Severity.CRITICAL,
        "opened_at": 100.0,
        "timeline": [TimelineEntry(t=100.0, service="database", event="opened")],
        "patterns": [
            PatternRef(
                pattern_id="abc", template="Connection pool exhausted after <NUM> ms", count=7,
                is_new=True, surge_ratio=8.0, service="database",
            )
        ],
        "hint": "Database or downstream dependency",
        "samples": ["Connection pool exhausted after 3004 ms member_id=[REDACTED]"],
    }  # fmt: skip
    return Incident(**{**base, **kw})  # type: ignore[arg-type]


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


def test_incident_round_trips_through_the_store(store: Store) -> None:
    incident = make_incident()
    store.upsert_incident(incident)
    assert store.get_incident("inc-1") == incident
    assert store.get_incident("missing") is None


def test_upsert_replaces_the_previous_version(store: Store) -> None:
    store.upsert_incident(make_incident())
    store.upsert_incident(
        make_incident(
            state=IncidentState.RESOLVED, resolved_at=200.0, current_severity=Severity.LOW
        )
    )
    (only,) = store.list_incidents()
    assert only.state is IncidentState.RESOLVED and only.resolved_at == 200.0


def test_list_incidents_filters_by_state_and_orders_newest_first(store: Store) -> None:
    store.upsert_incident(make_incident("old", opened_at=1.0, state=IncidentState.RESOLVED))
    store.upsert_incident(make_incident("new", opened_at=9.0))
    store.upsert_incident(make_incident("mid", opened_at=5.0, state=IncidentState.RESOLVED))
    assert [i.id for i in store.list_incidents()] == ["new", "mid", "old"]
    assert [i.id for i in store.list_incidents("all")] == ["new", "mid", "old"]
    assert [i.id for i in store.list_incidents("open")] == ["new"]
    assert [i.id for i in store.list_incidents("RESOLVED")] == ["mid", "old"]


def test_events_keep_every_transition_in_order(store: Store) -> None:
    incident = make_incident()
    store.add_event("inc-1", 1.0, "opened", Severity.LOW, incident)
    store.add_event("inc-1", 2.0, "severity_changed", Severity.HIGH, incident)
    store.add_event("inc-1", 3.0, "resolved", None, {"note": "plain dict snapshot"})
    store.add_event("other", 1.0, "opened", Severity.LOW, incident)
    events = store.events("inc-1")
    assert [(e["ts"], e["kind"], e["severity"]) for e in events] == [
        (1.0, "opened", Severity.LOW),
        (2.0, "severity_changed", Severity.HIGH),
        (3.0, "resolved", None),
    ]
    assert events[0]["snapshot"]["service"] == "database"
    assert events[2]["snapshot"] == {"note": "plain dict snapshot"}


def test_notifications_are_stored_with_channel_and_status(store: Store) -> None:
    store.add_notification(1.0, "inc-1", "sns", "sent", {"subject": "x"})
    store.add_notification(2.0, "inc-1", "cloudwatch", "failed", {"subject": "x"})
    store.add_notification(3.0, "inc-2", "dryrun", "written", {"subject": "y"})
    assert [(n["channel"], n["status"]) for n in store.notifications("inc-1")] == [
        ("sns", "sent"),
        ("cloudwatch", "failed"),
    ]
    assert len(store.notifications()) == 3
    assert store.notifications("inc-2")[0]["payload"] == {"subject": "y"}


def test_frames_are_returned_in_time_order(store: Store) -> None:
    store.add_frame("grp-1", 3.0, {"database": {"error_rate": 0.3}})
    store.add_frame("grp-1", 1.0, {"database": {"error_rate": 0.1}})
    store.add_frame("grp-2", 2.0, {})
    assert [f["t"] for f in store.frames("grp-1")] == [1.0, 3.0]
    assert store.frames("grp-1")[1]["data"] == {"database": {"error_rate": 0.3}}


def test_file_store_persists_across_reopen_and_creates_its_folder(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "sentinel.db"
    first = Store(path)
    first.upsert_incident(make_incident())
    first.add_event("inc-1", 1.0, "opened", Severity.LOW, make_incident())
    first.close()
    second = Store(path)
    assert second.get_incident("inc-1") == make_incident()
    assert len(second.events("inc-1")) == 1
    second.close()


def test_schema_creation_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    Store(path).close()
    Store(path).close()
