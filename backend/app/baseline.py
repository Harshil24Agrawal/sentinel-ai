"""Robust baseline (median/MAD) per service and metric, frozen during incidents."""

from __future__ import annotations

import statistics
from collections import deque

from app.clock import Clock
from app.config import Settings, get_settings
from app.models import BaselineView, WindowStats

MAD_TO_SIGMA = 1.4826  # makes MAD comparable to a standard deviation for normal data

ERROR_RATE = "error_rate"
P95_LATENCY = "p95_latency"
VOLUME = "volume"


class Baseline:
    """What "normal" looks like for each service, learned from window values.

    Samples are appended once per tick, but only when the window is full (a half-filled window
    has a biased volume) and the service is not in an open incident (freeze: otherwise the
    outage becomes the new normal). Median and MAD are recomputed lazily, once per appended
    sample, over at most ``BASELINE_HISTORY_SAMPLES`` values.
    """

    def __init__(self, clock: Clock, settings: Settings | None = None) -> None:
        self._clock = clock
        self._settings = settings or get_settings()
        self._history: dict[tuple[str, str], deque[float]] = {}
        self._stats_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._ready_at: dict[str, float] = {}

    # ---- learning ---------------------------------------------------------------------

    def update(
        self,
        service: str,
        window: WindowStats,
        *,
        frozen: bool = False,
        window_full: bool = True,
    ) -> bool:
        """Learn from this tick's window. Returns whether a sample was appended."""
        if frozen or not window_full:
            return False
        self._append(service, ERROR_RATE, window.error_rate)
        self._append(service, VOLUME, float(window.volume))
        if (
            window.p95_latency is not None
            and window.latency_samples >= self._settings.LATENCY_MIN_SAMPLES
        ):
            self._append(service, P95_LATENCY, window.p95_latency)
        if service not in self._ready_at and self.ready(service):
            self._ready_at[service] = self._clock.now()
        return True

    def _append(self, service: str, metric: str, value: float) -> None:
        key = (service, metric)
        history = self._history.get(key)
        if history is None:
            history = self._history[key] = deque(maxlen=self._settings.BASELINE_HISTORY_SAMPLES)
        history.append(value)
        self._stats_cache.pop(key, None)

    # ---- state ------------------------------------------------------------------------

    def samples(self, service: str, metric: str = ERROR_RATE) -> int:
        return len(self._history.get((service, metric), ()))

    def ready(self, service: str, metric: str = ERROR_RATE) -> bool:
        """Enough history to judge this metric (the first window has filled and
        ``WARMUP_SAMPLES`` samples have been taken)."""
        return self.samples(service, metric) >= self._settings.WARMUP_SAMPLES

    def ready_at(self, service: str) -> float | None:
        """Clock time at which the service finished warming up (None if it has not)."""
        return self._ready_at.get(service)

    # ---- statistics -------------------------------------------------------------------

    def _stats(self, service: str, metric: str) -> tuple[float, float] | None:
        key = (service, metric)
        cached = self._stats_cache.get(key)
        if cached is not None:
            return cached
        history = self._history.get(key)
        if not history:
            return None
        values = list(history)
        median = statistics.median(values)
        mad = statistics.median(abs(v - median) for v in values)
        self._stats_cache[key] = (median, mad)
        return median, mad

    def median(self, service: str, metric: str = ERROR_RATE) -> float | None:
        stats = self._stats(service, metric)
        return stats[0] if stats else None

    def mad(self, service: str, metric: str = ERROR_RATE) -> float | None:
        stats = self._stats(service, metric)
        return stats[1] if stats else None

    def _floor(self, metric: str) -> float:
        s = self._settings
        return {
            ERROR_RATE: s.ERROR_RATE_FLOOR,
            P95_LATENCY: s.LATENCY_FLOOR_MS,
            VOLUME: s.VOLUME_FLOOR,
        }[metric]

    def scale(self, service: str, metric: str = ERROR_RATE) -> float:
        """``1.4826 * MAD + floor``: the unit robust z-scores are measured in."""
        stats = self._stats(service, metric)
        mad = stats[1] if stats else 0.0
        return MAD_TO_SIGMA * mad + self._floor(metric)

    def robust_z(self, service: str, metric: str, x: float) -> float:
        """``(x - median) / (1.4826 * MAD + floor)``; 0 when there is no history yet."""
        median = self.median(service, metric)
        if median is None:
            return 0.0
        return (x - median) / self.scale(service, metric)

    def band(self, service: str, metric: str, z: float | None = None) -> tuple[float, float]:
        """The normal range ``median ± z * scale`` (z defaults to OPEN_Z), floored at 0."""
        median = self.median(service, metric)
        if median is None:
            return (0.0, 0.0)
        z = self._settings.OPEN_Z if z is None else z
        half = z * self.scale(service, metric)
        return (max(0.0, median - half), median + half)

    def error_rate_band(self, service: str) -> tuple[float, float]:
        """Normal error-rate range. An alert needs BOTH z >= OPEN_Z and an absolute rise of
        ABS_FLOOR_ERROR, so the upper edge is whichever of the two is larger."""
        median = self.median(service, ERROR_RATE)
        if median is None:
            return (0.0, 0.0)
        s = self._settings
        half = max(s.OPEN_Z * self.scale(service, ERROR_RATE), s.ABS_FLOOR_ERROR)
        return (max(0.0, median - half), min(1.0, median + half))

    def view(self, service: str) -> BaselineView:
        return BaselineView(
            error_rate_median=self.median(service, ERROR_RATE) or 0.0,
            error_rate_band=self.error_rate_band(service),
            p95_median=self.median(service, P95_LATENCY),
            ready=self.ready(service),
        )
