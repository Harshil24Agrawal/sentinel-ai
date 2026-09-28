import pytest
from helpers import Harness, services_graph

from app.correlation import CorrelationEngine, DependencyGraph, Member
from app.models import IncidentState

DB, APPT, NOTIF = "database", "appointment-service", "notification-service"
AUTH, CLAIMS, PROVIDER, PORTAL = (
    "auth-service",
    "claims-service",
    "provider-service",
    "patient-portal",
)


@pytest.fixture
def graph() -> DependencyGraph:
    return services_graph()


def member(service: str, opened_at: float, deviated_at: float | None = None) -> Member:
    return Member(
        f"inc-{service}", service, opened_at, opened_at if deviated_at is None else deviated_at
    )


# ---- the dependency graph ---------------------------------------------------------------


def test_adjacency_works_in_both_directions(graph: DependencyGraph) -> None:
    assert graph.adjacent(APPT, DB) and graph.adjacent(DB, APPT)
    assert graph.adjacent(NOTIF, APPT)
    assert not graph.adjacent(NOTIF, DB)  # only connected through appointment-service
    assert not graph.adjacent(AUTH, PROVIDER)  # share a dependency, but no edge


def test_transitive_dependents_and_dependencies(graph: DependencyGraph) -> None:
    assert graph.dependents(DB) == {AUTH, APPT, CLAIMS, PROVIDER, PORTAL}
    assert graph.dependents(NOTIF) == {APPT, PORTAL}
    assert graph.dependents(PORTAL) == set()
    assert graph.dependencies(APPT) == {DB, NOTIF}
    assert graph.dependencies(PORTAL) == {AUTH, APPT, DB, NOTIF}
    assert DB not in graph.dependents(DB)


def test_graph_survives_cycles_and_unknown_dependencies() -> None:
    g = DependencyGraph({"a": ["b"], "b": ["a", "ghost"]})
    assert g.dependents("a") == {"b"} and g.dependencies("a") == {"b", "ghost"}
    assert set(g.services) == {"a", "b", "ghost"}


# ---- joining groups ---------------------------------------------------------------------


def test_connected_incidents_join_one_group(graph: DependencyGraph) -> None:
    engine = CorrelationEngine(graph, window_seconds=180)
    g1, _ = engine.join(member(DB, 0), now=0)
    g2, _ = engine.join(member(APPT, 10), now=10)
    g3, _ = engine.join(member(NOTIF, 40), now=40)  # adjacent to appointment, not to database
    assert g1 is g2 is g3
    assert g1.services == [DB, APPT, NOTIF]
    assert len(engine.open_groups()) == 1


def test_unconnected_incidents_get_separate_groups(graph: DependencyGraph) -> None:
    engine = CorrelationEngine(graph, 180)
    g1, _ = engine.join(member(NOTIF, 0), now=0)
    g2, _ = engine.join(member(PROVIDER, 5), now=5)
    g3, _ = engine.join(member(AUTH, 6), now=6)  # database is healthy: auth/provider don't connect
    assert len({g1.id, g2.id, g3.id}) == 3


def test_grouping_needs_a_direct_edge_to_a_member_not_just_a_shared_hub(
    graph: DependencyGraph,
) -> None:
    engine = CorrelationEngine(graph, 180)
    a, _ = engine.join(member(AUTH, 0), now=0)
    b, _ = engine.join(member(CLAIMS, 3), now=3)  # both depend on database, which is fine
    assert a is not b


def test_correlation_window_is_respected(graph: DependencyGraph) -> None:
    engine = CorrelationEngine(graph, window_seconds=180)
    g1, _ = engine.join(member(DB, 0), now=0)
    inside, _ = engine.join(member(APPT, 180), now=180)  # exactly 180s after database: joins
    assert inside is g1
    # claims is adjacent to database, but database opened 181s ago; appointment (1s ago) is
    # not adjacent to claims. So no member is both recent and connected: a new group.
    outside, _ = engine.join(member(CLAIMS, 181), now=181)
    assert outside is not g1


def test_window_is_measured_against_group_members(graph: DependencyGraph) -> None:
    engine = CorrelationEngine(graph, window_seconds=180)
    g1, _ = engine.join(member(DB, 0), now=0)
    late, _ = engine.join(member(PROVIDER, 400), now=400)  # connected, but 400s later
    assert late is not g1


def test_a_resolved_group_is_not_joined(graph: DependencyGraph) -> None:
    engine = CorrelationEngine(graph, 180)
    g1, _ = engine.join(member(DB, 0), now=0)
    g1.resolved_at = 20
    g2, _ = engine.join(member(APPT, 30), now=30)
    assert g2 is not g1


def test_bridging_incident_merges_groups(graph: DependencyGraph) -> None:
    engine = CorrelationEngine(graph, 180)
    a, _ = engine.join(member(NOTIF, 0), now=0)
    b, _ = engine.join(member(DB, 5), now=5)  # not adjacent to notification
    assert a is not b
    a.notified, b.notified_severity = True, None
    merged, moved = engine.join(member(APPT, 10), now=10)  # adjacent to BOTH
    assert merged is a  # the older group wins
    assert moved == [f"inc-{DB}"]
    assert merged.services == [NOTIF, DB, APPT]
    assert len(engine.open_groups()) == 1
    assert engine.group_of(f"inc-{DB}") is merged


# ---- origin -----------------------------------------------------------------------------


def make_group(graph: DependencyGraph, *members: Member):
    engine = CorrelationEngine(graph, 10_000)
    group = None
    for m in members:
        group, _ = engine.join(m, now=m.opened_at)
    assert group is not None
    return engine, group


def test_origin_is_the_most_upstream_member(graph: DependencyGraph) -> None:
    engine, group = make_group(graph, member(APPT, 0), member(DB, 10), member(CLAIMS, 20))
    assert engine.origin(group) == DB  # appointment deviated first, but database is upstream


def test_origin_with_only_downstream_members(graph: DependencyGraph) -> None:
    engine, group = make_group(graph, member(APPT, 0), member(NOTIF, 40))
    assert engine.origin(group) == NOTIF


def test_origin_ties_go_to_whoever_deviated_first(graph: DependencyGraph) -> None:
    # database and notification are both upstream of appointment-service, one dependent each
    engine, group = make_group(
        graph, member(DB, 100, deviated_at=0), member(APPT, 105), member(NOTIF, 110, deviated_at=50)
    )
    assert engine.origin(group) == DB
    engine, group = make_group(
        graph, member(NOTIF, 100, deviated_at=0), member(APPT, 105), member(DB, 110, deviated_at=50)
    )
    assert engine.origin(group) == NOTIF


def test_single_member_group_is_its_own_origin(graph: DependencyGraph) -> None:
    engine, group = make_group(graph, member(PORTAL, 0))
    assert engine.origin(group) == PORTAL


# ---- blast radius -----------------------------------------------------------------------


def statuses(radius) -> dict[str, str]:
    return {n.service: n.status for n in radius.nodes}


def test_blast_radius_marks_impacted_at_risk_and_healthy(graph: DependencyGraph) -> None:
    engine = CorrelationEngine(graph, 180)
    radius = engine.blast_radius(DB, impacted=[DB, APPT, CLAIMS, NOTIF])
    assert radius.origin == DB
    assert statuses(radius) == {
        DB: "impacted",
        APPT: "impacted",
        CLAIMS: "impacted",
        NOTIF: "impacted",  # has an incident even though it does not depend on the database
        AUTH: "at_risk",
        PROVIDER: "at_risk",
        PORTAL: "at_risk",  # transitively, via auth-service and appointment-service
    }


def test_blast_radius_only_reaches_downstream(graph: DependencyGraph) -> None:
    engine = CorrelationEngine(graph, 180)
    radius = engine.blast_radius(NOTIF, impacted=[NOTIF])
    assert statuses(radius) == {
        NOTIF: "impacted",
        APPT: "at_risk",
        PORTAL: "at_risk",
        DB: "healthy",
        AUTH: "healthy",
        CLAIMS: "healthy",
        PROVIDER: "healthy",
    }


# ---- through the incident manager -------------------------------------------------------


def test_database_appointment_notification_form_one_group_with_database_as_origin() -> None:
    h = Harness(graph=services_graph())
    h.run(5, {DB: 6.0})
    h.run(10, {DB: 9.0, APPT: 9.0})
    h.run(30, {DB: 9.0, APPT: 9.0, NOTIF: 9.0})
    incidents = {i.service: i for i in h.manager.incidents()}
    assert set(incidents) == {DB, APPT, NOTIF}
    assert h.manager.groups_opened == 1
    assert len({i.group_id for i in incidents.values()}) == 1
    for inc in incidents.values():
        assert inc.origin == DB
        assert inc.affected_services == [DB, APPT, NOTIF]
        assert statuses(inc.blast_radius) == {  # type: ignore[arg-type]
            DB: "impacted",
            APPT: "impacted",
            NOTIF: "impacted",
            AUTH: "at_risk",
            CLAIMS: "at_risk",
            PROVIDER: "at_risk",
            PORTAL: "at_risk",
        }


def test_incident_story_merges_every_members_timeline_in_time_order() -> None:
    h = Harness(graph=services_graph())
    h.run(5, {DB: 6.0})
    h.run(10, {DB: 6.0, APPT: 6.0})
    h.run(10, {DB: 6.0, APPT: 6.0, NOTIF: 6.0})
    story = h.incident(NOTIF).story
    assert [e.t for e in story] == sorted(e.t for e in story)
    opened = [(e.service) for e in story if e.event == "opened"]
    assert opened == [DB, APPT, NOTIF]  # the outage as it spread
    assert story == h.incident(DB).story  # every member shows the same story


def test_unconnected_anomalies_are_separate_outages() -> None:
    h = Harness(graph=services_graph())
    h.run(10, {NOTIF: 9.0, PROVIDER: 9.0})
    assert h.manager.groups_opened == 2
    assert h.incident(NOTIF).origin == NOTIF and h.incident(PROVIDER).origin == PROVIDER


def test_group_severity_bump_and_single_notification_for_a_cascade() -> None:
    """Three CRITICAL incidents in one outage: announced once, then one resolve message."""
    h = Harness(graph=services_graph())
    h.run(5, {DB: 15.0})
    h.run(5, {DB: 15.0, APPT: 15.0})
    h.run(5, {DB: 15.0, APPT: 15.0, CLAIMS: 15.0})
    h.run(30, {DB: 0.0, APPT: 0.0, CLAIMS: 0.0})
    assert [n.kind for n in h.notifies()] == ["opened", "resolved"]
    assert {i.state for i in h.manager.incidents()} == {IncidentState.RESOLVED}
    assert h.notifies()[0].payload["origin"] == DB


def test_correlated_members_escalate_by_one_level() -> None:
    h = Harness(graph=services_graph())
    h.run(5, {DB: 4.0})
    assert h.incident(DB).current_severity.name == "LOW"
    h.run(5, {DB: 4.0, APPT: 4.0})  # two anomalous services: +1 level for each
    assert h.incident(DB).current_severity.name == "MEDIUM"
    assert h.incident(APPT).current_severity.name == "MEDIUM"


def test_resolved_members_stay_in_the_group_story_while_others_continue() -> None:
    h = Harness(graph=services_graph())
    h.run(5, {DB: 4.0, APPT: 4.0})
    h.run(30, {DB: 4.0, APPT: 0.0})  # appointment resolves first
    assert h.incident(APPT).state is IncidentState.RESOLVED
    assert h.incident(DB).state is IncidentState.OPEN
    assert h.manager.group(h.incident(DB).group_id).is_open  # type: ignore[union-attr]
    assert any(e.event == "resolved" and e.service == APPT for e in h.incident(DB).story)
