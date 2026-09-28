import pytest
from helpers import make_settings

from app.baseline import ERROR_RATE, MAD_TO_SIGMA, P95_LATENCY, VOLUME, Baseline
from app.clock import FakeClock
from app.models import WindowStats


def window(
    error_rate: float = 0.0, volume: int = 100, p95: float | None = None, samples: int = 0
) -> WindowStats:
    return WindowStats(
        volume=volume, error_rate=error_rate, p95_latency=p95, latency_samples=samples
    )


def make_baseline(clock: FakeClock, **settings: object) -> Baseline:
    return Baseline(clock, make_settings(**settings))


def feed(baseline: Baseline, values: list[float], service: str = "svc") -> None:
    for v in values:
        baseline.update(service, window(error_rate=v))


def test_median_and_mad_odd_count(clock: FakeClock) -> None:
    b = make_baseline(clock)
    feed(b, [0.01, 0.02, 0.03, 0.04, 1.0])  # the outlier must not move the median
    assert b.median("svc", ERROR_RATE) == pytest.approx(0.03)
    # |x - 0.03| = [0.02, 0.01, 0, 0.01, 0.97] -> median 0.01
    assert b.mad("svc", ERROR_RATE) == pytest.approx(0.01)


def test_median_and_mad_even_count(clock: FakeClock) -> None:
    b = make_baseline(clock)
    feed(b, [1, 2, 3, 4, 5, 100])  # median 3.5; deviations 2.5 1.5 .5 .5 1.5 96.5 -> MAD 1.5
    assert b.median("svc", ERROR_RATE) == 3.5
    assert b.mad("svc", ERROR_RATE) == 1.5


def test_robust_z_formula_and_floor(clock: FakeClock) -> None:
    b = make_baseline(clock)
    feed(b, [0.01, 0.02, 0.03, 0.04, 1.0])
    scale = MAD_TO_SIGMA * 0.01 + 0.005  # error-rate floor
    assert b.scale("svc", ERROR_RATE) == pytest.approx(scale)
    assert b.robust_z("svc", ERROR_RATE, 0.10) == pytest.approx((0.10 - 0.03) / scale)
    assert b.robust_z("svc", ERROR_RATE, 0.0) < 0  # below normal is negative

    flat = make_baseline(clock)
    feed(flat, [0.02] * 10, "flat")  # MAD = 0: the floor alone sets the scale
    assert flat.mad("flat", ERROR_RATE) == 0
    assert flat.robust_z("flat", ERROR_RATE, 0.03) == pytest.approx(0.01 / 0.005)


def test_each_metric_has_its_own_floor(clock: FakeClock) -> None:
    b = make_baseline(clock)
    for _ in range(5):
        b.update("svc", window(0.02, volume=100, p95=200.0, samples=50))
    assert b.scale("svc", ERROR_RATE) == pytest.approx(0.005)
    assert b.scale("svc", P95_LATENCY) == pytest.approx(20.0)
    assert b.scale("svc", VOLUME) == pytest.approx(5.0)


def test_outliers_do_not_drag_the_baseline(clock: FakeClock) -> None:
    b = make_baseline(clock)
    feed(b, [0.02] * 80 + [0.9] * 20)
    assert b.median("svc", ERROR_RATE) == pytest.approx(0.02)  # a mean would say ~0.2


def test_history_is_capped(clock: FakeClock) -> None:
    b = make_baseline(clock, BASELINE_HISTORY_SAMPLES=50)
    feed(b, [0.01] * 500)
    assert b.samples("svc") == 50


def test_history_does_not_grow_while_frozen(clock: FakeClock) -> None:
    b = make_baseline(clock)
    feed(b, [0.02] * 40)
    assert b.samples("svc") == 40
    median_before = b.median("svc", ERROR_RATE)
    for _ in range(100):
        assert b.update("svc", window(0.9, p95=900.0, samples=50), frozen=True) is False
    assert b.samples("svc") == 40
    assert b.samples("svc", VOLUME) == 40
    assert b.median("svc", ERROR_RATE) == median_before  # the outage was not learned
    assert b.update("svc", window(0.02)) is True  # learning resumes when the incident ends
    assert b.samples("svc") == 41


def test_freeze_is_per_service(clock: FakeClock) -> None:
    b = make_baseline(clock)
    for _ in range(10):
        b.update("a", window(0.02), frozen=True)
        b.update("b", window(0.02))
    assert (b.samples("a"), b.samples("b")) == (0, 10)


def test_nothing_is_learned_from_a_partial_window(clock: FakeClock) -> None:
    b = make_baseline(clock)
    assert b.update("svc", window(0.5), window_full=False) is False
    assert b.samples("svc") == 0
    assert b.ready("svc") is False


def test_ready_only_after_warmup_samples(clock: FakeClock) -> None:
    b = make_baseline(clock, WARMUP_SAMPLES=30)
    feed(b, [0.02] * 29)
    assert not b.ready("svc")
    assert b.ready_at("svc") is None
    clock.advance(7)
    feed(b, [0.02])
    assert b.ready("svc")
    assert b.ready_at("svc") == clock.now()
    clock.advance(50)
    feed(b, [0.02] * 5)
    assert b.ready_at("svc") == clock.now() - 50  # recorded once, at the moment it became ready


def test_ready_survives_a_freeze(clock: FakeClock) -> None:
    b = make_baseline(clock, WARMUP_SAMPLES=5)
    feed(b, [0.02] * 5)
    for _ in range(50):
        b.update("svc", window(0.9), frozen=True)
    assert b.ready("svc")


def test_latency_history_needs_enough_samples_per_window(clock: FakeClock) -> None:
    b = make_baseline(clock, WARMUP_SAMPLES=3)
    for _ in range(5):
        b.update("svc", window(0.02, p95=120.0, samples=19))  # below LATENCY_MIN_SAMPLES (20)
    assert b.samples("svc", P95_LATENCY) == 0
    assert b.median("svc", P95_LATENCY) is None
    assert b.ready("svc") and not b.ready("svc", P95_LATENCY)
    for _ in range(3):
        b.update("svc", window(0.02, p95=120.0, samples=20))
    assert b.ready("svc", P95_LATENCY)
    assert b.median("svc", P95_LATENCY) == 120.0


def test_no_history_means_no_stats(clock: FakeClock) -> None:
    b = make_baseline(clock)
    assert b.median("svc", ERROR_RATE) is None
    assert b.mad("svc", ERROR_RATE) is None
    assert b.robust_z("svc", ERROR_RATE, 0.5) == 0.0
    assert b.band("svc", ERROR_RATE) == (0.0, 0.0)


def test_band_is_median_plus_minus_z_scales(clock: FakeClock) -> None:
    b = make_baseline(clock, OPEN_Z=3.0)
    feed(b, [0.01, 0.02, 0.03, 0.04, 1.0])
    scale = MAD_TO_SIGMA * 0.01 + 0.005
    assert b.band("svc", ERROR_RATE, z=1.0) == pytest.approx((0.03 - scale, 0.03 + scale))
    lo, hi = b.band("svc", ERROR_RATE)  # default z is OPEN_Z = 3
    assert hi == pytest.approx(0.03 + 3 * scale)
    assert lo == 0.0  # 0.03 - 3 * scale is negative: clamped, an error rate can't go below 0


def test_error_band_reflects_the_absolute_floor(clock: FakeClock) -> None:
    b = make_baseline(clock, OPEN_Z=3.0, ABS_FLOOR_ERROR=0.02)
    feed(b, [0.02] * 10)  # z part: 3 * 0.005 = 0.015 < 0.02, so the floor sets the edge
    assert b.error_rate_band("svc") == pytest.approx((0.0, 0.04))
    view = b.view("svc")
    assert view.error_rate_median == pytest.approx(0.02)
    assert view.error_rate_band == pytest.approx((0.0, 0.04))
    assert view.p95_median is None and view.ready is False


def test_band_follows_the_sensitivity_setting(clock: FakeClock) -> None:
    b = make_baseline(clock, OPEN_Z=3.0)
    feed(b, [0.01, 0.02, 0.03, 0.04, 1.0])
    narrow = b.band("svc", ERROR_RATE)
    b._settings.OPEN_Z = 5.0
    wide = b.band("svc", ERROR_RATE)
    assert wide[1] > narrow[1]
