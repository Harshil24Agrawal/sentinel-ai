import json

import pytest

from app.explainer.rule_based import RuleBasedExplainer, format_duration
from app.hints import FALLBACK_HINT, SILENCE_HINT
from app.models import (
    GroupCard,
    IncidentState,
    MemberIncident,
    PatternRef,
    Severity,
    Signal,
    SignalType,
    TimelineEntry,
)

DB_HINT = "Database or downstream dependency: check DB health, latency and connection pool."


def card(**overrides: object) -> GroupCard:
    base: dict[str, object] = {
        "id": "grp-1",
        "state": IncidentState.OPEN,
        "current_severity": Severity.CRITICAL,
        "max_severity": Severity.CRITICAL,
        "opened_at": 100.0,
        "origin": "database",
        "affected_services": ["database", "appointment-service", "claims-service"],
        "has_new_pattern": True,
        "what": "p95 latency 1200 ms vs normal 12 ms; error rate 25.0% vs normal 0.5%",
        "duration_s": 130.0,
        "failed_ops": 812,
        "hint": DB_HINT,
        "patterns": [
            PatternRef(
                pattern_id="a1",
                template="Connection pool exhausted after <NUM> ms",
                count=60,
                expected_per_min=0.0,
                is_new=True,
                surge_ratio=61.0,
                service="database",
            ),
            PatternRef(
                pattern_id="b2",
                template="Database timeout while saving claim <NUM>",
                count=90,
                expected_per_min=0.0,
                is_new=True,
                surge_ratio=91.0,
                service="claims-service",
            ),
        ],
        "samples": ["Connection pool exhausted after 3004 ms member_id=[REDACTED]"],
    }
    return GroupCard(**{**base, **overrides})  # type: ignore[arg-type]


explainer = RuleBasedExplainer()


def test_explains_what_where_how_bad_and_why() -> None:
    e = explainer.explain(card())
    assert e.is_incident and e.source == "rule_based"
    assert e.summary.startswith("database is the probable origin of a CRITICAL incident")
    assert "appointment-service and claims-service" in e.summary
    assert "p95 latency 1200 ms vs normal 12 ms" in e.summary  # the numbers behind the alert
    assert "2m 10s" in e.summary and "812 failed operations" in e.summary
    assert "NEW pattern 'Connection pool exhausted after <NUM> ms' on database" in e.summary
    assert "seen 60x" in e.summary and "normally 0/min" in e.summary
    assert e.affected_services == ["database", "appointment-service", "claims-service"]


def test_the_cause_is_always_probable_never_root() -> None:
    e = explainer.explain(card())
    assert e.probable_cause.startswith("Probable cause (not confirmed): " + DB_HINT)
    assert "Database timeout while saving claim <NUM>" in e.probable_cause  # also seen
    everything = " ".join([e.summary, e.probable_cause, *e.recommended_checks, e.label])
    assert "root cause" not in everything.lower()


def test_same_card_same_words() -> None:
    a, b = explainer.explain(card()), explainer.explain(card())
    assert a == b
    assert json.dumps(a.model_dump()) == json.dumps(b.model_dump())


def test_recommended_checks_are_ordered_and_specific() -> None:
    checks = explainer.explain(card()).recommended_checks
    assert checks[0] == DB_HINT
    assert "Look at database first" in checks[1]
    assert "appointment-service and claims-service" in checks[2]
    assert checks[-1].startswith("Compare with recent deployments")


def test_single_service_incident_has_no_downstream_step() -> None:
    e = explainer.explain(card(affected_services=["database"], origin="database"))
    assert e.summary.startswith("database is the probable origin of a CRITICAL incident.")
    assert not any("Then check" in c for c in e.recommended_checks)


def test_resolved_incident_is_described_in_the_past_tense() -> None:
    e = explainer.explain(
        card(state=IncidentState.RESOLVED, current_severity=Severity.LOW, resolved_at=300.0)
    )
    assert "It lasted 2m 10s and has resolved; the worst severity was CRITICAL." in e.summary
    assert "failed operations in the current window" not in e.summary


def test_silent_service_gets_the_silence_advice() -> None:
    silence = Signal(
        type=SignalType.SILENCE, service="notification-service", value=0, baseline_median=300, z=20
    )
    member = MemberIncident(
        id="i1",
        service="notification-service",
        state=IncidentState.OPEN,
        current_severity=Severity.CRITICAL,
        max_severity=Severity.CRITICAL,
        opened_at=1.0,
        signals=[silence],
    )
    e = explainer.explain(
        card(
            origin="notification-service",
            affected_services=["notification-service"],
            patterns=[],
            hint=SILENCE_HINT,
            what="0 log lines in the last 20s vs about 100 normally",
            incidents=[member],
        )
    )
    assert e.probable_cause == f"Probable cause (not confirmed): {SILENCE_HINT}"
    assert any("running and shipping logs" in c for c in e.recommended_checks)
    assert e.confidence == "high"  # silent and CRITICAL


def test_silence_seen_only_in_the_story_still_counts() -> None:
    story = [TimelineEntry(t=1.0, service="s", event="service went silent")]
    e = explainer.explain(
        card(origin="s", affected_services=["s"], patterns=[], hint=SILENCE_HINT, story=story)
    )
    assert any("running and shipping logs" in c for c in e.recommended_checks)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "high"),  # signature + NEW pattern + several services
        ({"affected_services": ["database"]}, "medium"),  # signature + NEW pattern
        ({"has_new_pattern": False}, "medium"),  # signature + several services
        ({"hint": FALLBACK_HINT}, "medium"),  # NEW pattern + several services
        ({"affected_services": ["database"], "has_new_pattern": False}, "low"),
        (
            {"hint": FALLBACK_HINT, "has_new_pattern": False, "affected_services": ["database"]},
            "low",
        ),
    ],
)
def test_confidence_reflects_how_much_independent_evidence_agrees(
    overrides: dict[str, object], expected: str
) -> None:
    assert explainer.explain(card(**overrides)).confidence == expected


def test_without_a_matching_signature_it_says_so() -> None:
    e = explainer.explain(card(hint="", patterns=[]))
    assert FALLBACK_HINT in e.probable_cause
    assert not any(c == FALLBACK_HINT for c in e.recommended_checks)


def test_the_explanation_never_repeats_sample_lines() -> None:
    """Samples are redacted at source, but the explanation should not need them at all."""
    dirty = "Lookup failed for member_id=MBR1234567 ssn=123-45-6789"
    e = explainer.explain(card(samples=[dirty]))
    text = json.dumps(e.model_dump())
    assert "MBR1234567" not in text and "123-45-6789" not in text and dirty not in text


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (45.4, "45s"),
        (59.6, "1m 00s"),
        (130, "2m 10s"),
        (3600, "1h 00m"),
        (3900, "1h 05m"),
    ],
)
def test_format_duration(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text
