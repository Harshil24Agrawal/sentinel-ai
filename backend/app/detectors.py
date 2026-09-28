"""Statistical detectors: error_rate, latency, silence, new_pattern, pattern_surge."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from app.aggregator import Aggregator
from app.baseline import ERROR_RATE, P95_LATENCY, VOLUME, Baseline
from app.config import Settings, get_settings
from app.models import Signal, SignalType, WindowStats


@dataclass(frozen=True, slots=True)
class Evaluation:
    """One service, one tick: the signals that fired plus the raw measurements behind them.

    The measurements matter even when nothing fired: the incident manager needs them to
    decide when an incident may resolve (z below RESOLVE_Z, absolute floor not exceeded).
    ``None`` means "not measurable this tick" (not ready, or too little data).
    """

    service: str
    ready: bool
    signals: list[Signal] = field(default_factory=list)
    error_z: float | None = None
    error_delta: float | None = None  # window error rate minus baseline median
    latency_z: float | None = None
    shortfall_z: float | None = None  # how far volume is BELOW normal (positive = lower)
    volume_ratio: float | None = None  # recent volume / expected volume (1.0 = normal)

    def is_calm(
        self, resolve_z: float, abs_floor_error: float, silence_resolve_ratio: float = 0.5
    ) -> bool:
        """True when every detector is back near normal (used for hysteresis, so the bar is
        RESOLVE_Z, lower than the OPEN_Z that raised the alarm). Volume is released on a ratio
        rather than a z: a busy service's window volume is noisier than its (short-history)
        MAD suggests, and a z-based release kept resetting the calm streak."""
        if not self.ready or self.signals:
            return False
        if self.error_z is not None and self.error_z >= resolve_z:
            return False
        if self.error_delta is not None and self.error_delta >= abs_floor_error:
            return False
        if self.latency_z is not None and self.latency_z >= resolve_z:
            return False
        return not (self.volume_ratio is not None and self.volume_ratio < silence_resolve_ratio)


class Detectors:
    """Compares each service's current window with its baseline.

    Pure with respect to time: everything time-dependent lives in the aggregator and baseline,
    which take the Clock. Thresholds are read from settings on every call, so the sensitivity
    slider (``OPEN_Z``) takes effect immediately.
    """

    def __init__(
        self, aggregator: Aggregator, baseline: Baseline, settings: Settings | None = None
    ) -> None:
        self._agg = aggregator
        self._baseline = baseline
        self._settings = settings or get_settings()

    def run(self, frozen: Collection[str] = ()) -> dict[str, Evaluation]:
        """Evaluate every service, THEN let the baseline learn from this tick, so a sample is
        never judged against a baseline that already contains it. Call after
        ``Aggregator.tick(frozen)``."""
        results: dict[str, Evaluation] = {}
        for service in self._agg.services():
            window = self._agg.window(service)
            results[service] = self.evaluate(service, window)
            self._baseline.update(
                service,
                window,
                frozen=service in frozen,
                window_full=self._agg.window_full(service),
            )
        return results

    def evaluate(self, service: str, window: WindowStats | None = None) -> Evaluation:
        if window is None:
            window = self._agg.window(service)
        if not self._baseline.ready(service):
            return Evaluation(service, ready=False)

        signals: list[Signal] = []
        error_z, error_delta = self._error_rate(service, window, signals)
        latency_z = self._latency(service, window, signals)
        shortfall_z, volume_ratio = self._silence(service, signals)
        if window.volume >= self._settings.MIN_VOLUME:  # tiny samples never page anyone
            self._patterns(service, window, signals)
        return Evaluation(
            service, True, signals, error_z, error_delta, latency_z, shortfall_z, volume_ratio
        )

    # ---- individual detectors ---------------------------------------------------------

    def _error_rate(
        self, service: str, w: WindowStats, out: list[Signal]
    ) -> tuple[float | None, float | None]:
        s = self._settings
        median = self._baseline.median(service, ERROR_RATE)
        if median is None or w.volume < s.MIN_VOLUME:
            return None, None
        z = self._baseline.robust_z(service, ERROR_RATE, w.error_rate)
        delta = w.error_rate - median
        if z >= s.OPEN_Z and delta >= s.ABS_FLOOR_ERROR:
            out.append(
                Signal(
                    type=SignalType.ERROR_RATE,
                    service=service,
                    value=w.error_rate,
                    baseline_median=median,
                    z=z,
                    detail=f"error rate {w.error_rate:.1%} vs normal {median:.1%}",
                )
            )
        return z, delta

    def _latency(self, service: str, w: WindowStats, out: list[Signal]) -> float | None:
        s = self._settings
        median = self._baseline.median(service, P95_LATENCY)
        if (
            median is None
            or not self._baseline.ready(service, P95_LATENCY)
            or w.p95_latency is None
            or w.latency_samples < s.LATENCY_MIN_SAMPLES
        ):
            return None
        z = self._baseline.robust_z(service, P95_LATENCY, w.p95_latency)
        if z >= s.OPEN_Z and w.p95_latency >= s.LATENCY_MIN_RATIO * median:
            out.append(
                Signal(
                    type=SignalType.LATENCY,
                    service=service,
                    value=w.p95_latency,
                    baseline_median=median,
                    z=z,
                    detail=f"p95 latency {w.p95_latency:.0f} ms vs normal {median:.0f} ms",
                )
            )
        return z

    def _silence(self, service: str, out: list[Signal]) -> tuple[float | None, float | None]:
        """Lines in the lookback (default: the whole window) below SILENCE_RATIO of normal.

        ``Signal.z`` is the shortfall in robust units, oriented so bigger = worse.
        """
        s = self._settings
        median = self._baseline.median(service, VOLUME)
        if median is None or median < s.MIN_VOLUME:
            return None, None
        lookback = min(s.SILENCE_LOOKBACK_SECONDS, s.WINDOW_SECONDS)
        fraction = lookback / s.WINDOW_SECONDS
        expected = median * fraction
        recent = self._agg.recent_volume(service, lookback)
        z = (expected - recent) / (self._baseline.scale(service, VOLUME) * fraction)
        if recent < s.SILENCE_RATIO * expected:
            out.append(
                Signal(
                    type=SignalType.SILENCE,
                    service=service,
                    value=float(recent),
                    baseline_median=expected,
                    z=z,
                    detail=(
                        f"{recent} log lines in the last {lookback}s "
                        f"vs about {expected:.0f} normally"
                    ),
                )
            )
        return z, recent / expected

    def _patterns(self, service: str, w: WindowStats, out: list[Signal]) -> None:
        """NEW: first seen after warm-up. SURGE: far above the pattern's long-run rate.

        Neither has a robust z (z stays 0): they raise confidence and feed probable cause,
        and on their own open at most a LOW incident. The numbers are in value/baseline_median.
        """
        s = self._settings
        ready_at = self._baseline.ready_at(service)
        for pid, count in w.pattern_counts.items():
            first_seen = self._agg.first_seen(service, pid)
            if first_seen is None or ready_at is None:
                continue
            template = self._agg.template(service, pid) or pid
            if first_seen > ready_at and count >= s.NEW_PATTERN_MIN_COUNT:
                out.append(
                    Signal(
                        type=SignalType.NEW_PATTERN,
                        service=service,
                        value=float(count),
                        baseline_median=0.0,
                        z=0.0,
                        detail=f"new pattern x{count}: {template}",
                        pattern_id=pid,
                    )
                )
            expected = self._agg.expected_count(service, pid)
            ratio = (count + 1) / (expected + 1)
            if count >= s.SURGE_MIN_COUNT and ratio >= s.SURGE_RATIO:
                out.append(
                    Signal(
                        type=SignalType.PATTERN_SURGE,
                        service=service,
                        value=float(count),
                        baseline_median=expected,
                        z=0.0,
                        detail=(
                            f"pattern surge x{count} vs about {expected:.1f} normally: {template}"
                        ),
                        pattern_id=pid,
                    )
                )
