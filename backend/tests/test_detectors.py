from helpers import WARM, Feed, Rig

from app.models import SignalType
from app.patterns import mine

ERR = SignalType.ERROR_RATE
LAT = SignalType.LATENCY
SIL = SignalType.SILENCE
NEW = SignalType.NEW_PATTERN
SURGE = SignalType.PATTERN_SURGE


def steady(*names: str) -> list[Feed]:
    return [Feed(n, lines=25, p_error=0.02) for n in names]


# ---- warm-up and false positives --------------------------------------------------------


def test_nothing_fires_while_warming_up() -> None:
    rig = Rig(Feed("svc", lines=25, p_error=1.0))  # 100% errors from the very first line
    results = rig.run(85)
    assert all(not r["svc"].ready and not r["svc"].signals for r in results)


def test_service_becomes_ready_after_a_window_plus_warmup_samples() -> None:
    rig = Rig(Feed("svc", lines=25))
    results = rig.run(WARM)
    first_ready = next(i for i, r in enumerate(results) if r["svc"].ready)
    assert 85 <= first_ready <= 92  # 60s window to fill + 30 samples


def test_steady_noisy_traffic_raises_no_signals() -> None:
    feeds = [
        Feed("a", lines=25, p_error=0.02),
        Feed("b", lines=8, p_error=0.01),
        Feed("c", lines=40, p_error=0.005, latency=(8, 3)),
    ]
    rig = Rig(*feeds)
    rig.run(250)
    assert sum(r["a"].ready for r in rig.history) > 150
    for service in "abc":
        assert rig.signals(service) == [], service


# ---- error rate -------------------------------------------------------------------------


def test_sustained_error_spike_produces_signals() -> None:
    feed = Feed("svc", lines=25, p_error=0.02)
    rig = Rig(feed)
    rig.run(WARM)
    assert rig.signals("svc") == []

    feed.p_error = 0.50  # a big sustained step
    rig.run(30)
    first = rig.first_signal_tick("svc", ERR)
    assert first is not None and first - WARM < 10  # detected within 10 simulated seconds

    signals = rig.signals("svc", ERR, since=WARM)
    assert all(sig.z >= 3.0 for _, sig in signals)
    assert all(sig.value - sig.baseline_median >= 0.02 for _, sig in signals)
    assert abs(signals[0][1].baseline_median - 0.02) < 0.01
    assert "vs normal" in signals[0][1].detail
    # still signalling on every tick of the sustained spike (the incident manager needs that)
    assert {i for i, _ in signals} == set(range(first, WARM + 30))
    assert rig.history[-1]["svc"].signals and not rig.history[-1]["svc"].is_calm(1.5, 0.02)


def test_a_slower_step_takes_longer_but_is_still_detected() -> None:
    feed = Feed("svc", lines=25, p_error=0.02)
    rig = Rig(feed)
    rig.run(WARM)
    feed.p_error = 0.30
    rig.run(30)
    first = rig.first_signal_tick("svc", ERR)
    assert first is not None and 5 <= first - WARM <= 12  # the 60s window smooths the step


def test_a_rise_below_the_absolute_floor_is_ignored_even_if_z_is_large() -> None:
    feed = Feed("svc", lines=25, p_error=0.0, noisy=False)  # zero noise: median 0, MAD 0
    rig = Rig(feed)
    warm = 400  # long enough that the slow rise cannot shift the median before z peaks
    rig.run(warm)
    feed.p_error = 0.018  # +1.8 points: z = 3.6 but below ABS_FLOOR_ERROR (2 points)
    rig.run(100)
    z_values = [r["svc"].error_z for r in rig.history[warm:]]
    assert max(z for z in z_values if z is not None) >= 3.0  # z alone would have fired
    assert rig.signals("svc", ERR) == []


def test_three_second_blip_is_absorbed_by_the_sliding_window() -> None:
    """At 25% errors for 3s the 60s window only moves ~1 point: no error_rate signal at all
    (so, unlike a bigger blip, persistence is not even needed to keep it quiet)."""
    feed = Feed("svc", lines=25, p_error=0.015)
    rig = Rig(feed)
    rig.run(WARM)
    feed.p_error = 0.25
    rig.run(3)
    feed.p_error = 0.015
    rig.run(120)
    assert rig.signals("svc", ERR) == []
    assert max(r["svc"].error_z or 0 for r in rig.history[WARM:]) < 3.0


def test_sensitivity_changes_take_effect_immediately() -> None:
    feed = Feed("svc", lines=25, p_error=0.02)
    rig = Rig(feed)
    rig.run(WARM)
    rig.settings.OPEN_Z = 100.0
    feed.p_error = 0.50
    rig.run(40)
    assert rig.signals("svc", ERR) == []
    rig.settings.OPEN_Z = 3.0
    rig.tick()
    assert rig.signals("svc", ERR, since=len(rig.history) - 1)


def test_low_volume_service_below_min_volume_produces_no_signals() -> None:
    tiny = Feed("tiny", lines=1, every=3, p_error=0.0)  # ~20 lines per window (< MIN_VOLUME)
    rig = Rig(tiny, *steady("busy"))
    rig.run(WARM)
    assert rig.agg.window("tiny").volume < 30

    tiny.p_error = 1.0  # every line an error, with a brand-new message
    tiny.error_message = "Totally new failure {n}"
    rig.run(120)
    assert rig.agg.window("tiny").volume < 30
    assert rig.signals("tiny") == []  # no error_rate, no NEW pattern, nothing
    assert all(r["tiny"].error_z is None for r in rig.history[WARM:])

    tiny.stop = rig.t  # and going quiet is not silence either: its normal volume is tiny
    rig.run(150)
    assert rig.signals("tiny") == []


# ---- latency ----------------------------------------------------------------------------


def test_latency_spike_produces_a_latency_signal() -> None:
    feed = Feed("svc", lines=25, p_error=0.02, latency=(100, 10))
    rig = Rig(feed)
    rig.run(WARM)
    feed.latency = (400, 20)
    rig.run(30)
    first = rig.first_signal_tick("svc", LAT)
    assert first is not None and first - WARM < 10
    signal = rig.signals("svc", LAT, since=WARM)[0][1]
    assert signal.value >= 1.5 * signal.baseline_median
    assert signal.z >= 3.0
    assert rig.signals("svc", ERR) == []  # only latency moved


def test_latency_needs_a_real_change_not_just_a_large_z() -> None:
    feed = Feed("svc", lines=25, latency=(100, 0.5))  # very stable: tiny MAD
    rig = Rig(feed)
    rig.run(WARM)
    feed.latency = (140, 0.5)  # +40 ms: z ~2 with the 20 ms floor, and < 1.5x anyway
    rig.run(90)
    assert rig.signals("svc", LAT) == []


def test_no_latency_signal_without_enough_latency_samples() -> None:
    feed = Feed("svc", lines=25, latency=None)  # lines carry no latency_ms at all
    rig = Rig(feed)
    rig.run(WARM)
    assert not rig.baseline.ready("svc", "p95_latency")
    rig.run(30)
    assert rig.signals("svc", LAT) == []
    assert all(r["svc"].latency_z is None for r in rig.history)


# ---- silence ----------------------------------------------------------------------------


def test_silence_is_detected() -> None:
    feed = Feed("quiet", lines=10)
    rig = Rig(feed, *steady("other"))
    rig.run(WARM)
    assert rig.signals("quiet") == []

    feed.stop = rig.t  # stops logging entirely
    rig.run(80)
    first = rig.first_signal_tick("quiet", SIL)
    assert first is not None
    # default lookback is 20s: fires once the last 20s hold < 20% of the normal volume
    assert 14 <= first - WARM <= 20
    signal = rig.signals("quiet", SIL)[0][1]
    assert signal.value < 0.2 * signal.baseline_median
    assert signal.z >= 3.0  # oriented so bigger = worse
    assert rig.signals("other") == []  # the rest of the fleet is fine
    assert rig.signals("quiet", ERR) == []  # a quiet service is not "errors"


def test_whole_window_lookback_is_the_slower_spec_behaviour() -> None:
    feed = Feed("quiet", lines=10)
    rig = Rig(feed, SILENCE_LOOKBACK_SECONDS=60)  # "window volume < 20% of median", literally
    rig.run(WARM)
    feed.stop = rig.t
    rig.run(80)
    first = rig.first_signal_tick("quiet", SIL)
    assert first is not None and 45 <= first - WARM <= 55
    assert rig.signals("quiet", SIL, since=WARM - 5)[0][0] >= WARM  # and not before it stopped


def test_silence_clears_when_the_service_resumes() -> None:
    feed = Feed("quiet", lines=10, stop=WARM)
    rig = Rig(feed)
    rig.run(WARM + 80)
    assert rig.history[-1]["quiet"].signals  # silent by now
    feed.stop = None
    feed.start = rig.t
    rig.run(80)
    last = rig.history[-1]["quiet"]
    assert not last.signals and last.is_calm(1.5, 0.02)


def test_an_unexpected_lull_in_a_normally_busy_service_is_not_silence_at_half_volume() -> None:
    feed = Feed("svc", lines=20)
    rig = Rig(feed)
    rig.run(WARM)
    feed.lines = 10  # half the normal volume is not "silent" (< 20% is)
    rig.run(120)
    assert rig.signals("svc", SIL) == []
    # it is measured (the incident manager sees a shortfall) before the baseline adapts
    shortfalls = [r["svc"].shortfall_z for r in rig.history[WARM:]]
    assert max(z for z in shortfalls if z is not None) >= 3


# ---- new patterns and surges ------------------------------------------------------------


def test_new_pattern_is_flagged_only_after_warm_up() -> None:
    base = Feed("svc", lines=25, p_error=0.02)
    # first appears at tick 30, during warm-up (ready at ~90): part of "normal"
    early = Feed("svc", lines=1, p_error=1.0, error_message="Early quirk {n}", start=30)
    # first appears at tick 120, well after warm-up
    late = Feed("svc", lines=1, p_error=1.0, error_message="Brand new failure {n}", start=120)
    # only two lines ever: below the count threshold
    brief = Feed(
        "svc", lines=1, p_error=1.0, error_message="Fleeting oddity {n}", start=130, stop=132
    )
    rig = Rig(base, early, late, brief)
    rig.run(220)

    new = rig.signals("svc", NEW)
    by_pattern = {sig.pattern_id for _, sig in new}
    assert by_pattern == {mine("Brand new failure 1").id}
    first_tick, signal = new[0]
    assert 120 < first_tick <= 124  # count reaches 3 within a few seconds
    assert signal.value >= 3 and signal.z == 0.0
    assert "Brand new failure <NUM>" in signal.detail
    assert mine("Early quirk 1").id not in by_pattern
    assert mine("Fleeting oddity 1").id not in by_pattern
    assert rig.signals("svc", ERR) == []  # a new pattern alone is not an error-rate anomaly


def test_a_pattern_from_before_warm_up_never_counts_as_new_even_if_it_surges_later() -> None:
    base = Feed("svc", lines=25, p_error=0.02)
    rare = Feed("svc", lines=1, every=10, p_error=1.0, error_message="Rare failure {n}", start=20)
    rig = Rig(base, rare)
    rig.run(WARM)
    rare.every = 1
    rig.run(120)
    assert rig.signals("svc", NEW) == []


def test_pattern_surge_needs_ten_lines_and_five_times_normal() -> None:
    base = Feed("svc", lines=25, p_error=0.02)
    rare = Feed("svc", lines=1, every=10, p_error=1.0, error_message="Rare failure {n}", start=5)
    rig = Rig(base, rare)
    rig.run(WARM)  # ~6 per window: the long-run expectation
    assert rig.signals("svc", SURGE) == []
    rare.every = 1  # ten times the normal rate
    rig.run(90)

    surges = rig.signals("svc", SURGE, since=WARM)
    assert surges
    pid = mine("Rare failure 1").id
    assert {sig.pattern_id for _, sig in surges} == {pid}
    for _, sig in surges:
        assert sig.value >= 10
        assert (sig.value + 1) / (sig.baseline_median + 1) >= 5.0
        assert sig.z == 0.0
    # it took a while: the count has to reach ~5x normal, and never fired below 10
    assert surges[0][0] - WARM > 20
    assert rig.signals("svc", NEW) == []


def test_pattern_detectors_stay_quiet_for_normal_steady_patterns() -> None:
    rig = Rig(Feed("svc", lines=25, p_error=0.02))
    rig.run(300)
    assert rig.signals("svc", SURGE) == [] and rig.signals("svc", NEW) == []


# ---- freeze and calm --------------------------------------------------------------------


def test_frozen_service_does_not_learn_the_outage() -> None:
    feed = Feed("svc", lines=25, p_error=0.02)
    rig = Rig(feed)
    rig.run(WARM)
    median_before = rig.baseline.median("svc")
    samples_before = rig.baseline.samples("svc")

    feed.p_error = 0.50
    rig.frozen.add("svc")  # what the incident manager will do once an incident opens
    rig.run(200)
    assert rig.baseline.samples("svc") == samples_before
    assert rig.baseline.median("svc") == median_before
    assert rig.history[-1]["svc"].signals  # so the outage is still an outage 200s later


def test_without_the_freeze_a_long_outage_becomes_normal() -> None:
    """The reason the freeze exists: after ~900s of learning, the outage is the baseline."""
    feed = Feed("svc", lines=25, p_error=0.02)
    rig = Rig(feed, BASELINE_HISTORY_SAMPLES=80)
    rig.run(WARM)
    feed.p_error = 0.50
    rig.run(250)
    assert not rig.history[-1]["svc"].signals
    assert rig.baseline.median("svc") > 0.3


def test_service_is_calm_again_only_after_the_window_drains() -> None:
    feed = Feed("svc", lines=25, p_error=0.02)
    rig = Rig(feed)
    rig.run(WARM)
    feed.p_error = 0.50
    rig.frozen.add("svc")
    rig.run(20)
    assert not rig.history[-1]["svc"].is_calm(1.5, 0.02)

    feed.p_error = 0.02  # recovery
    rig.run(20)
    assert not rig.history[-1]["svc"].is_calm(1.5, 0.02)  # the window still remembers the outage
    rig.run(60)
    assert rig.history[-1]["svc"].is_calm(1.5, 0.02)


def test_unready_service_is_never_calm() -> None:
    rig = Rig(Feed("svc", lines=25))
    rig.run(10)
    assert not rig.history[-1]["svc"].is_calm(1.5, 0.02)


def test_services_are_judged_independently() -> None:
    a, b = Feed("a", lines=25), Feed("b", lines=25)
    rig = Rig(a, b)
    rig.run(WARM)
    a.p_error = 0.5
    rig.run(30)
    assert rig.signals("a", ERR) and rig.signals("b") == []
