import math
import random

import pytest
from helpers import make_event, make_settings

from app.aggregator import Aggregator
from app.clock import FakeClock
from app.models import LogEvent

MESSAGES = ("Timeout {n}", "Disk slow {n}", "Odd thing {n}", "Retry {n}")


def make_agg(clock: FakeClock, **settings: object) -> Aggregator:
    return Aggregator(clock, make_settings(**settings))


def random_event(rng: random.Random, service: str) -> LogEvent:
    roll = rng.random()
    return make_event(
        service,
        rng.choice(MESSAGES).format(n=rng.randint(1, 999)),
        error=roll < 0.15,
        warn=0.15 <= roll < 0.25,
        latency=rng.choice([None, rng.randint(1, 500)]),
    )


def test_incremental_window_matches_brute_force(clock: FakeClock) -> None:
    window = 10
    agg = make_agg(clock, WINDOW_SECONDS=window)
    rng = random.Random(5)
    per_tick: dict[str, list[list[LogEvent]]] = {"a": [], "b": []}
    for _ in range(45):
        for service in per_tick:
            events = [random_event(rng, service) for _ in range(rng.randint(0, 25))]
            per_tick[service].append(events)
            for e in events:
                agg.add(e)
        agg.tick()
        clock.advance(1)
        for service, ticks in per_tick.items():
            events = [e for chunk in ticks[-window:] for e in chunk]
            stats = agg.window(service)
            lat = sorted(e.latency_ms for e in events if e.latency_ms is not None)
            assert stats.volume == len(events)
            assert stats.errors == sum(e.is_error for e in events)
            assert stats.warns == sum(e.is_warn for e in events)
            assert stats.error_rate == pytest.approx(stats.errors / len(events) if events else 0)
            assert stats.latency_samples == len(lat)
            assert stats.p95_latency == (lat[math.ceil(0.95 * len(lat)) - 1] if lat else None)
            expected_patterns: dict[str, int] = {}
            for e in events:
                if e.pattern_id:
                    expected_patterns[e.pattern_id] = expected_patterns.get(e.pattern_id, 0) + 1
            assert stats.pattern_counts == expected_patterns
            assert stats.distinct_error_patterns == len(
                {e.pattern_id for e in events if e.is_error}
            )


def test_window_is_full_only_after_a_whole_window_of_ticks(clock: FakeClock) -> None:
    agg = make_agg(clock, WINDOW_SECONDS=5)
    agg.add(make_event("svc", "hello"))
    for _ in range(4):
        agg.tick()
        assert not agg.window_full("svc")
    agg.tick()
    assert agg.window_full("svc")
    assert not agg.window_full("nobody")


def test_lines_leave_the_window_after_window_seconds(clock: FakeClock) -> None:
    agg = make_agg(clock, WINDOW_SECONDS=5)
    for _ in range(3):
        agg.add(make_event("svc", "hello", error=True))
    for _ in range(5):
        agg.tick()
    # the bucket holding the lines was sealed on tick 1, so tick 6 evicts it
    assert agg.window("svc").volume == 3
    agg.tick()
    stats = agg.window("svc")
    assert (stats.volume, stats.errors, stats.pattern_counts, stats.latency_samples) == (
        0,
        0,
        {},
        0,
    )
    assert stats.distinct_error_patterns == 0


def test_a_silent_service_keeps_being_sealed(clock: FakeClock) -> None:
    agg = make_agg(clock, WINDOW_SECONDS=10)
    for _ in range(10):
        agg.add(make_event("svc", "hello"))
        agg.tick()
    assert agg.window("svc").volume == 10
    for _ in range(4):
        agg.tick()
    assert agg.window("svc").volume == 6
    assert agg.recent_volume("svc", 3) == 0
    assert agg.recent_volume("svc", 100) == 6  # capped at the window


def test_p95_is_nearest_rank(clock: FakeClock) -> None:
    agg = make_agg(clock)
    for ms in range(1, 101):
        agg.add(make_event("svc", "ok", latency=ms))
    agg.tick()
    assert agg.window("svc").p95_latency == 95.0
    assert agg.window("svc").latency_samples == 100


def test_lines_without_latency_are_excluded_from_latency_stats(clock: FakeClock) -> None:
    agg = make_agg(clock)
    agg.add(make_event("svc", "ok"))
    agg.tick()
    stats = agg.window("svc")
    assert (stats.volume, stats.p95_latency, stats.latency_samples) == (1, None, 0)


def test_unparsed_lines_are_counted_but_never_bucketed(clock: FakeClock) -> None:
    agg = make_agg(clock)
    junk = make_event("unknown", "garbage").model_copy(update={"parsed": False})
    for _ in range(5):
        agg.add(junk)
    agg.tick()
    assert agg.unparsed == 5
    assert agg.services() == []


def test_only_error_and_warn_lines_get_pattern_counts(clock: FakeClock) -> None:
    agg = make_agg(clock)
    agg.add(make_event("svc", "all good 1"))
    agg.add(make_event("svc", "trouble 2", error=True))
    agg.add(make_event("svc", "trouble 3", error=True))
    agg.add(make_event("svc", "hmm 4", warn=True))
    agg.tick()
    stats = agg.window("svc")
    assert sorted(stats.pattern_counts.values()) == [1, 2]
    assert stats.distinct_error_patterns == 1  # the warn pattern is not an error pattern


def test_first_seen_and_template_survive_leaving_the_window(clock: FakeClock) -> None:
    agg = make_agg(clock, WINDOW_SECONDS=3)
    event = make_event("svc", "Timeout 17", error=True)
    clock.advance(42)
    agg.add(event)
    for _ in range(5):
        agg.tick()
        clock.advance(1)
    assert agg.window("svc").pattern_counts == {}
    assert agg.first_seen("svc", event.pattern_id or "") == 1_000_042.0
    assert agg.template("svc", event.pattern_id or "") == "Timeout <NUM>"
    assert agg.first_seen("svc", "nope") is None
    assert agg.first_seen("other", event.pattern_id or "") is None  # per service


def test_expected_count_is_seeded_from_the_first_full_window(clock: FakeClock) -> None:
    agg = make_agg(clock, WINDOW_SECONDS=10)
    pid = make_event("svc", "Timeout 1", error=True).pattern_id or ""
    for _ in range(10):
        agg.add(make_event("svc", "Timeout 1", error=True))
        agg.add(make_event("svc", "Disk slow 2", error=True))  # a different pattern
        assert agg.expected_count("svc", pid) == 0.0  # window not full yet
        agg.tick()
    assert agg.expected_count("svc", pid) == 10.0  # 10 lines in the first full window


def test_expected_count_learns_slowly_and_freezes(clock: FakeClock) -> None:
    agg = make_agg(clock, WINDOW_SECONDS=10, BASELINE_HISTORY_SAMPLES=99)
    pid = make_event("svc", "Timeout 1", error=True).pattern_id or ""
    for _ in range(10):
        agg.add(make_event("svc", "Timeout 1", error=True))
        agg.tick()
    seeded = agg.expected_count("svc", pid)
    assert seeded == 10.0

    for _ in range(10):  # 10x surge: window count climbs to 100, the long-run rate lags behind
        for _ in range(10):
            agg.add(make_event("svc", "Timeout 1", error=True))
        agg.tick()
    learned = agg.expected_count("svc", pid)
    assert seeded < learned < 30  # alpha = 2/100 per tick

    for _ in range(10):
        for _ in range(10):
            agg.add(make_event("svc", "Timeout 1", error=True))
        agg.tick(frozen={"svc"})
    assert agg.expected_count("svc", pid) == learned


def test_pattern_tracking_is_capped_but_counting_is_not(clock: FakeClock) -> None:
    agg = make_agg(clock, MAX_PATTERNS_PER_SERVICE=2)
    for word in ("alpha", "bravo", "charlie"):
        agg.add(make_event("svc", f"{word} broke", error=True))
    agg.tick()
    charlie = make_event("svc", "charlie broke", error=True).pattern_id or ""
    assert agg.window("svc").distinct_error_patterns == 3
    assert agg.first_seen("svc", charlie) is None


def test_samples_are_recent_error_lines_only(clock: FakeClock) -> None:
    agg = make_agg(clock)
    agg.add(make_event("svc", "fine 1"))
    for i in range(5):
        agg.add(make_event("svc", f"Timeout {i}", error=True))
    agg.add(make_event("svc", "Other 9", error=True))
    assert agg.samples("svc") == ["Timeout 3", "Timeout 4", "Other 9"]
    other = make_event("svc", "Other 1", error=True).pattern_id or ""
    assert agg.samples("svc", pattern_ids={other}) == ["Other 9"]
    assert agg.samples("nobody") == []


def test_latency_samples_per_bucket_are_capped(clock: FakeClock) -> None:
    agg = make_agg(clock)
    for ms in range(1, 1001):
        agg.add(make_event("svc", "ok", latency=ms))
    agg.tick()
    stats = agg.window("svc")
    assert stats.volume == 1000
    assert stats.latency_samples == 200


def test_unknown_service_has_an_empty_window(clock: FakeClock) -> None:
    stats = make_agg(clock).window("nobody")
    assert (stats.volume, stats.error_rate, stats.p95_latency) == (0, 0.0, None)
