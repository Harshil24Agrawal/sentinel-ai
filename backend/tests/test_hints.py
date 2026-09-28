import pytest
from helpers import WARM, Feed, Rig, make_event, make_settings

from app.aggregator import Aggregator
from app.hints import (
    DEFAULT_RULES,
    FALLBACK_HINT,
    SILENCE_HINT,
    hint_for,
    merge_evidence,
    rank_patterns,
    top_patterns,
)
from app.models import PatternRef, Signal, SignalType
from app.patterns import mine

DB_HINT = DEFAULT_RULES[0][1]
AUTH_HINT = DEFAULT_RULES[1][1]
RESOURCE_HINT = DEFAULT_RULES[2][1]
DOWNSTREAM_HINT = DEFAULT_RULES[3][1]


def agg_with(*lines: tuple[str, str]) -> tuple[Aggregator, list[PatternRef]]:
    """An aggregator holding (service, error message) lines, and a PatternRef for each."""
    agg = Aggregator(Rig().clock, make_settings())
    refs = []
    for service, message in lines:
        agg.add(make_event(service, message, error=True))
        pattern = mine(message)
        refs.append(
            PatternRef(pattern_id=pattern.id, template=pattern.template, count=5, service=service)
        )
    agg.tick()
    return agg, refs


def silence_signal() -> Signal:
    return Signal(type=SignalType.SILENCE, service="s", value=0.0, baseline_median=100.0, z=9.0)


# ---- keyword rules ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Database timeout while saving appointment 88214", DB_HINT),
        ("Connection pool exhausted after 3004 ms", DB_HINT),
        ("Deadlock detected on table claims", DB_HINT),
        ("Connection refused by 10.0.0.5", DB_HINT),
        ("Token validation failed: signing key a1b2c3d4e5f6 not found", AUTH_HINT),
        ("Request rejected: HTTP 401 for request 5", AUTH_HINT),  # the 401 is only in the sample
        ("Login rejected with HTTP 403 for request 5", AUTH_HINT),
        ("Certificate expired for host x", AUTH_HINT),
        ("java.lang.OutOfMemoryError: Java heap space", RESOURCE_HINT),
        ("write failed: no space left on device", RESOURCE_HINT),
        ("Upstream returned HTTP 503 for request 9", DOWNSTREAM_HINT),  # 503 masked in template
        ("Circuit breaker open for claims", DOWNSTREAM_HINT),
        ("Provider unavailable", DOWNSTREAM_HINT),
        ("Cache miss ratio 5000 units", FALLBACK_HINT),  # \b5\d\d\b must not match 5000
        ("Something unexpected happened", FALLBACK_HINT),
    ],
)
def test_keyword_rules(message: str, expected: str) -> None:
    agg, refs = agg_with(("svc", message))
    assert hint_for(refs, [], agg, make_settings()) == expected


def test_rules_see_the_sample_not_just_the_masked_template() -> None:
    agg, refs = agg_with(("svc", "Upstream returned HTTP 503 for request 9"))
    assert "503" not in refs[0].template  # masked
    assert hint_for(refs, [], agg) == DOWNSTREAM_HINT


def test_top_ranked_pattern_decides_but_later_ones_are_a_fallback() -> None:
    agg, refs = agg_with(("svc", "Odd thing happened"), ("svc", "Database timeout while saving 5"))
    assert hint_for(refs, [], agg) == DB_HINT  # first pattern matches nothing, second does
    agg, refs = agg_with(("svc", "Database timeout while saving 5"), ("svc", "Token expired"))
    assert hint_for(refs, [], agg) == DB_HINT  # ranked first wins over a later match


def test_silence_hint_when_there_are_no_patterns() -> None:
    agg, _ = agg_with()
    assert hint_for([], [silence_signal()], agg) == SILENCE_HINT
    assert hint_for([], [], agg) == FALLBACK_HINT


def test_a_matching_pattern_beats_the_silence_hint() -> None:
    agg, refs = agg_with(("svc", "Database timeout while saving 5"))
    assert hint_for(refs, [silence_signal()], agg) == DB_HINT


def test_extra_rules_from_config_are_checked_first() -> None:
    settings = make_settings(HINT_RULES_EXTRA=[("certificate|tls", "Check the TLS chain.")])
    agg, refs = agg_with(("svc", "TLS certificate rejected"))
    assert hint_for(refs, [], agg, settings) == "Check the TLS chain."  # beats the auth rule
    assert hint_for(refs, [], agg, make_settings()) == AUTH_HINT
    agg, refs = agg_with(("svc", "Database timeout while saving 5"))
    assert hint_for(refs, [], agg, settings) == DB_HINT  # defaults still apply


def test_hint_text_is_a_probable_cause_never_a_root_cause() -> None:
    for _, hint in (*DEFAULT_RULES, ("", SILENCE_HINT), ("", FALLBACK_HINT)):
        assert "root cause" not in hint.lower()


# ---- ranking ----------------------------------------------------------------------------


def test_new_patterns_rank_above_surges_and_background_is_excluded() -> None:
    base = Feed("svc", lines=25, p_error=0.02)  # "Request {n} failed": ordinary background
    rare = Feed("svc", lines=1, every=10, p_error=1.0, error_message="Rare failure {n}", start=5)
    rig = Rig(base, rare)
    rig.run(WARM)
    rare.every = 1  # surge: ~10x its normal rate
    fresh = Feed("svc", lines=1, p_error=1.0, error_message="Brand new failure {n}", start=rig.t)
    rig.feeds.append(fresh)
    rig.run(14)

    ranked = rank_patterns(["svc"], rig.agg, rig.baseline, rig.settings)
    assert [p.template for p in ranked] == ["Brand new failure <NUM>", "Rare failure <NUM>"]
    new, surge = ranked
    assert new.is_new and not surge.is_new
    assert new.expected_per_min < 1.0  # normally ~0/min (a little slow learning after 14s)
    assert 3.0 <= surge.expected_per_min <= 9.0  # "normally ~6/min"
    assert surge.surge_ratio is not None and surge.surge_ratio >= 2.0
    assert new.count >= 3 and new.service == "svc"
    assert "Request <NUM> failed" not in {p.template for p in ranked}


def test_top_three_only() -> None:
    base = Feed("svc", lines=25, p_error=0.02)
    rig = Rig(base)
    rig.run(WARM)
    for i in range(5):
        rig.feeds.append(
            Feed(
                "svc",
                lines=1,
                p_error=1.0,
                error_message=f"Novel failure kind{i} {{n}}",
                start=rig.t,
            )
        )
    rig.run(8)
    assert len(rank_patterns(["svc"], rig.agg, rig.baseline, rig.settings)) == 3
    assert len(rank_patterns(["svc"], rig.agg, rig.baseline, rig.settings, limit=5)) == 5


def test_surges_rank_by_ratio() -> None:
    base = Feed("svc", lines=25, p_error=0.02)
    mild = Feed("svc", lines=1, every=6, p_error=1.0, error_message="Mild surge {n}", start=5)
    strong = Feed("svc", lines=1, every=12, p_error=1.0, error_message="Strong surge {n}", start=5)
    rig = Rig(base, mild, strong)
    rig.run(WARM)
    mild.every, strong.every = 3, 1  # 2x and 12x their normal rates
    rig.run(20)
    ranked = rank_patterns(["svc"], rig.agg, rig.baseline, rig.settings)
    assert [p.template for p in ranked][0] == "Strong surge <NUM>"


def test_ranking_spans_the_groups_services() -> None:
    a, b = Feed("a", lines=25), Feed("b", lines=25)
    rig = Rig(a, b)
    rig.run(WARM)
    rig.feeds.append(Feed("b", lines=1, p_error=1.0, error_message="Only b fails {n}", start=rig.t))
    rig.run(8)
    assert rank_patterns(["a"], rig.agg, rig.baseline, rig.settings) == []
    (only,) = rank_patterns(["a", "b"], rig.agg, rig.baseline, rig.settings)
    assert only.service == "b"


def test_nothing_interesting_means_no_patterns() -> None:
    rig = Rig(Feed("svc", lines=25, p_error=0.02))
    rig.run(WARM)
    assert rank_patterns(["svc"], rig.agg, rig.baseline, rig.settings) == []


# ---- evidence kept at its worst ---------------------------------------------------------


def ref(template: str, count: int, *, service: str = "svc", new: bool = False, ratio: float = 1.0):
    return PatternRef(
        pattern_id=mine(template).id,
        template=template,
        count=count,
        is_new=new,
        surge_ratio=ratio,
        service=service,
    )


def test_evidence_keeps_each_pattern_at_its_peak_count() -> None:
    peak: dict = {}
    merge_evidence(peak, [ref("Boom <NUM>", 10, new=True, ratio=11.0)])
    merge_evidence(peak, [ref("Boom <NUM>", 60, new=True, ratio=61.0)])
    merge_evidence(peak, [ref("Boom <NUM>", 4, new=False, ratio=5.0)])  # the window is draining
    (only,) = peak.values()
    assert (only.count, only.is_new, only.surge_ratio) == (60, True, 61.0)


def test_a_pattern_that_was_ever_new_stays_new() -> None:
    peak: dict = {}
    merge_evidence(peak, [ref("Boom <NUM>", 10, new=False, ratio=3.0)])
    merge_evidence(peak, [ref("Boom <NUM>", 8, new=True, ratio=9.0)])  # lower count, but NEW
    (only,) = peak.values()
    assert only.is_new and only.count == 10


def test_evidence_is_keyed_by_service_and_ranked_new_first() -> None:
    peak: dict = {}
    merge_evidence(
        peak,
        [
            ref("Same <NUM>", 5, service="a", ratio=2.0),
            ref("Same <NUM>", 9, service="b", ratio=4.0),
            ref("Fresh <NUM>", 3, service="a", new=True, ratio=4.0),
        ],
    )
    ranked = top_patterns(peak)
    assert [(p.service, p.template) for p in ranked] == [
        ("a", "Fresh <NUM>"),  # NEW beats a bigger surge
        ("b", "Same <NUM>"),
        ("a", "Same <NUM>"),
    ]
    assert len(top_patterns(peak, limit=2)) == 2


def test_rank_patterns_with_no_limit_returns_every_candidate() -> None:
    base = Feed("svc", lines=25, p_error=0.02)
    rig = Rig(base)
    rig.run(WARM)
    for i in range(5):
        rig.feeds.append(
            Feed("svc", lines=1, p_error=1.0, error_message=f"Novel kind{i} {{n}}", start=rig.t)
        )
    rig.run(8)
    assert len(rank_patterns(["svc"], rig.agg, rig.baseline, rig.settings, limit=None)) == 5
