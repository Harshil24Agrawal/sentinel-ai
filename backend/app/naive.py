"""Naive threshold detector: the comparison baseline. No grouping, no hysteresis."""

from __future__ import annotations

from app.aggregator import Aggregator
from app.clock import Clock
from app.config import Settings, get_settings


class NaiveDetector:
    """Every ``NAIVE_INTERVAL_SECONDS``, each service whose window error rate is above
    ``NAIVE_ERROR_THRESHOLD`` counts one alert. This is what an on-call engineer gets from a
    plain "error rate > 5%" rule: the same outage pages again every few seconds, per service."""

    def __init__(
        self, clock: Clock, aggregator: Aggregator, settings: Settings | None = None
    ) -> None:
        self._clock = clock
        self._agg = aggregator
        self._settings = settings or get_settings()
        self._last_check: float | None = None
        self.alerts = 0
        self.alerts_by_service: dict[str, int] = {}

    def tick(self) -> int:
        """Run a check if one is due. Returns the alerts raised by this call."""
        now = self._clock.now()
        if (
            self._last_check is not None
            and now - self._last_check < self._settings.NAIVE_INTERVAL_SECONDS
        ):
            return 0
        self._last_check = now
        raised = 0
        for service in self._agg.services():
            if self._agg.window(service).error_rate > self._settings.NAIVE_ERROR_THRESHOLD:
                raised += 1
                self.alerts_by_service[service] = self.alerts_by_service.get(service, 0) + 1
        self.alerts += raised
        return raised
