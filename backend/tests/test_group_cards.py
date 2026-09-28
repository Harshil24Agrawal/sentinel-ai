"""One card per outage (CLAUDE.md §7): group summaries and cards built by the manager."""

import json

from helpers import Harness, services_graph

from app.models import GroupCard, Incident, IncidentState, Severity

DB, APPT, NOTIF, PROVIDER = (
    "database",
    "appointment-service",
    "notification-service",
    "provider-service",
)


def cascade(h: Harness) -> None:
    """database, then appointment-service, then notification-service: one spreading outage."""
    h.run(5, {DB: 15.0})
    h.run(10, {DB: 15.0, APPT: 9.0})
    h.run(10, {DB: 15.0, APPT: 9.0, NOTIF: 6.0})


def test_one_card_for_the_whole_outage_with_members_nested() -> None:
    h = Harness(graph=services_graph())
    cascade(h)
    (summary,) = h.manager.group_summaries()
    assert summary.state is IncidentState.OPEN
    assert summary.origin == DB
    assert summary.affected_services == [DB, APPT, NOTIF]
    assert [m.service for m in summary.incidents] == [DB, APPT, NOTIF]  # ordered by opened_at
    assert summary.title.startswith("database: error rate 30.0%")
    assert summary.title.endswith("(+2 more services)")
    for member in summary.incidents:
        assert member.timeline and member.timeline[-1].event in {"opened", "escalated"}
        assert member.current_severity >= Severity.LOW
        assert member.state is IncidentState.OPEN


def test_summary_severity_is_the_worst_open_member() -> None:
    h = Harness(graph=services_graph())
    cascade(h)
    (summary,) = h.manager.group_summaries()
    assert summary.current_severity == Severity.CRITICAL
    assert summary.max_severity == Severity.CRITICAL
    severities = {m.service: m.current_severity for m in summary.incidents}
    assert severities[DB] == Severity.CRITICAL


def test_full_card_adds_the_story_blast_radius_and_explanation() -> None:
    h = Harness(graph=services_graph())
    cascade(h)
    (summary,) = h.manager.group_summaries()
    card = h.manager.group_card(summary.id)
    assert isinstance(card, GroupCard)
    assert card.id == summary.id and card.origin == DB
    assert [e.t for e in card.story] == sorted(e.t for e in card.story)
    assert {e.service for e in card.story} == {DB, APPT, NOTIF}
    assert [e.service for e in card.story if e.event == "opened"] == [DB, APPT, NOTIF]
    assert card.blast_radius is not None and card.blast_radius.origin == DB
    statuses = {n.service: n.status for n in card.blast_radius.nodes}
    assert statuses[DB] == statuses[APPT] == statuses[NOTIF] == "impacted"
    assert statuses["patient-portal"] == "at_risk"
    assert card.explanation is not None and card.explanation.source == "rule_based"
    assert card.explanation.summary.startswith("database is the probable origin")
    assert card.duration_s > 0 and card.peak_z >= 15


def test_group_card_is_found_by_group_id_or_by_member_incident_id() -> None:
    h = Harness(graph=services_graph())
    cascade(h)
    (summary,) = h.manager.group_summaries()
    by_member = h.manager.group_card(summary.incidents[1].id)
    assert by_member is not None and by_member.id == summary.id
    assert h.manager.resolve_group_id(summary.id) == summary.id
    assert h.manager.group_card("nope") is None
    assert h.manager.group_summary("nope") is None
    assert h.manager.resolve_group_id("nope") is None


def test_severity_travels_as_its_name_in_json() -> None:
    h = Harness(graph=services_graph())
    cascade(h)
    card = h.manager.group_card(h.manager.group_summaries()[0].id)
    assert card is not None
    data = json.loads(card.model_dump_json())
    assert data["current_severity"] == "CRITICAL" and data["max_severity"] == "CRITICAL"
    assert data["incidents"][0]["current_severity"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    assert data["incidents"][0]["timeline"][0]["t"] > 0
    assert data["state"] == "OPEN"
    assert {"hint", "patterns", "samples", "story", "blast_radius", "explanation"} <= data.keys()
    assert data["what_if"] is None and data["ml_score"] is None  # stretch, not built yet
    # and it parses back from those names
    assert GroupCard.model_validate_json(card.model_dump_json()) == card


def test_incident_json_round_trips_with_severity_names() -> None:
    h = Harness(graph=services_graph())
    cascade(h)
    incident = h.incident(DB)
    text = incident.model_dump_json()
    assert '"current_severity":"CRITICAL"' in text
    assert Incident.model_validate_json(text) == incident


def test_summaries_are_sorted_by_priority_and_filtered_by_state() -> None:
    h = Harness(graph=services_graph())
    h.run(5, {"claims-service": 6.0})  # a separate, lower-severity outage (MEDIUM)
    h.run(5, {"claims-service": 6.0, PROVIDER: 6.0})
    h.run(30, {"claims-service": 0.0, PROVIDER: 0.0})  # ...that resolves
    h.run(5, {NOTIF: 15.0})  # and a CRITICAL one that is still open
    open_only = h.manager.group_summaries("open")
    resolved = h.manager.group_summaries("resolved")
    assert [g.origin for g in open_only] == [NOTIF]
    assert [g.origin for g in resolved] == [PROVIDER]  # provider is upstream of claims
    assert [g.origin for g in h.manager.group_summaries()] == [NOTIF, PROVIDER]  # open first
    assert [g.origin for g in h.manager.group_summaries("ALL")] == [NOTIF, PROVIDER]


def test_resolved_group_card() -> None:
    h = Harness(graph=services_graph())
    cascade(h)
    h.run(30, {DB: 0.0, APPT: 0.0, NOTIF: 0.0})
    (summary,) = h.manager.group_summaries()
    assert summary.state is IncidentState.RESOLVED and summary.resolved_at is not None
    assert {m.state for m in summary.incidents} == {IncidentState.RESOLVED}
    card = h.manager.group_card(summary.id)
    assert card is not None and card.explanation is not None
    assert "has resolved; the worst severity was CRITICAL" in card.explanation.summary
    assert card.max_severity == Severity.CRITICAL
    assert summary.notified is True  # it was announced, and the card still says so


def test_notified_flag_only_after_a_high_severity_announcement() -> None:
    h = Harness(graph=services_graph())
    h.run(5, {DB: 4.0})  # LOW: nothing sent
    assert h.manager.group_summaries()[0].notified is False
    h.run(3, {DB: 15.0})
    assert h.manager.group_summaries()[0].notified is True


def test_cards_are_snapshots_not_live_objects() -> None:
    h = Harness(graph=services_graph())
    cascade(h)
    before = h.manager.group_card(h.manager.group_summaries()[0].id)
    assert before is not None
    h.run(30, {DB: 0.0, APPT: 0.0, NOTIF: 0.0})
    assert before.state is IncidentState.OPEN  # later ticks don't rewrite a card we hold
