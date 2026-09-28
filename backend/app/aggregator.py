"""Per-service 1-second buckets and sliding-window stats, maintained incrementally."""

from __future__ import annotations

import math
from bisect import bisect_left, insort
from collections import deque
from collections.abc import Collection
from dataclasses import dataclass, field
from itertools import islice

from app.clock import Clock
from app.config import Settings, get_settings
from app.models import LogEvent, WindowStats

MAX_LATENCIES_PER_BUCKET = 200  # later samples in a very busy second are dropped
MAX_SAMPLES = 20  # recent redacted error lines kept per service, for incident cards


@dataclass(slots=True)
class _Bucket:
    lines: int = 0
    errors: int = 0
    warns: int = 0
    latencies: list[int] = field(default_factory=list)
    patterns: dict[str, int] = field(default_factory=dict)  # error + warn lines
    error_patterns: dict[str, int] = field(default_factory=dict)  # error lines only


@dataclass(slots=True)
class _PatternInfo:
    template: str
    first_seen: float
    rate: float = 0.0  # long-run expected count per window (EWMA)


def _bump(counts: dict[str, int], key: str, n: int) -> None:
    value = counts.get(key, 0) + n
    if value <= 0:
        counts.pop(key, None)
    else:
        counts[key] = value


class _ServiceState:
    def __init__(self) -> None:
        self.open = _Bucket()
        self.buckets: deque[_Bucket] = deque()
        self.lines = self.errors = self.warns = 0
        self.patterns: dict[str, int] = {}
        self.error_patterns: dict[str, int] = {}
        self.latencies: list[int] = []  # sorted; the window's latency samples
        self.info: dict[str, _PatternInfo] = {}
        self.samples: deque[tuple[str, str]] = deque(maxlen=MAX_SAMPLES)  # (pattern_id, message)
        self.rates_seeded = False

    def seal(self, maxlen: int) -> None:
        """Close the open bucket into the window and evict the oldest one if the ring is full."""
        bucket, self.open = self.open, _Bucket()
        self.buckets.append(bucket)
        self._apply(bucket, +1)
        if len(self.buckets) > maxlen:
            self._apply(self.buckets.popleft(), -1)

    def _apply(self, b: _Bucket, sign: int) -> None:
        self.lines += sign * b.lines
        self.errors += sign * b.errors
        self.warns += sign * b.warns
        for pid, n in b.patterns.items():
            _bump(self.patterns, pid, sign * n)
        for pid, n in b.error_patterns.items():
            _bump(self.error_patterns, pid, sign * n)
        for ms in b.latencies:
            if sign > 0:
                insort(self.latencies, ms)
            else:
                del self.latencies[bisect_left(self.latencies, ms)]


class Aggregator:
    """Rolling per-service statistics over ``WINDOW_SECONDS`` of 1-second buckets.

    Lines are bucketed by *arrival* (the current tick), not by the timestamp in the line, so a
    skewed or missing log timestamp cannot distort the window. Each ``tick`` seals one bucket
    per known service and adds/subtracts whole buckets, so a tick costs O(buckets touched),
    independent of window length.
    """

    def __init__(self, clock: Clock, settings: Settings | None = None) -> None:
        self._clock = clock
        self._settings = settings or get_settings()
        self._states: dict[str, _ServiceState] = {}
        self.unparsed = 0

    @property
    def _maxlen(self) -> int:
        s = self._settings
        return max(1, round(s.WINDOW_SECONDS / s.TICK_SECONDS))

    def add(self, event: LogEvent) -> None:
        """Count a line in the current open bucket. Unparsed lines are counted, not bucketed:
        a burst of garbage must not create an "unknown" service that can later go silent."""
        if not event.parsed:
            self.unparsed += 1
            return
        state = self._states.get(event.service)
        if state is None:
            state = self._states[event.service] = _ServiceState()
        bucket = state.open
        bucket.lines += 1
        if event.is_error:
            bucket.errors += 1
        if event.is_warn:
            bucket.warns += 1
        if event.latency_ms is not None and len(bucket.latencies) < MAX_LATENCIES_PER_BUCKET:
            bucket.latencies.append(event.latency_ms)
        pid = event.pattern_id
        if pid is not None and (event.is_error or event.is_warn):
            bucket.patterns[pid] = bucket.patterns.get(pid, 0) + 1
            if event.is_error:
                bucket.error_patterns[pid] = bucket.error_patterns.get(pid, 0) + 1
                state.samples.append((pid, event.message))
            if pid not in state.info and len(state.info) < self._settings.MAX_PATTERNS_PER_SERVICE:
                state.info[pid] = _PatternInfo(event.template or "", self._clock.now())

    def tick(self, frozen: Collection[str] = ()) -> None:
        """Seal the current second for every service and refresh long-run pattern rates.

        ``frozen`` are services with an open incident: their pattern rates stop learning,
        like the baseline, so an outage does not become the new normal.
        """
        maxlen = self._maxlen
        alpha = 2.0 / (self._settings.BASELINE_HISTORY_SAMPLES + 1)
        for name, state in self._states.items():
            state.seal(maxlen)
            if len(state.buckets) < maxlen:
                continue
            if not state.rates_seeded:  # first full window: whatever is in it is "normal"
                for pid, info in state.info.items():
                    info.rate = float(state.patterns.get(pid, 0))
                state.rates_seeded = True
            elif name not in frozen:
                for pid, info in state.info.items():
                    info.rate += alpha * (state.patterns.get(pid, 0) - info.rate)

    # ---- queries ----------------------------------------------------------------------

    def services(self) -> list[str]:
        return list(self._states)

    def window_full(self, service: str) -> bool:
        state = self._states.get(service)
        return state is not None and len(state.buckets) >= self._maxlen

    def window(self, service: str) -> WindowStats:
        state = self._states.get(service)
        if state is None:
            return WindowStats()
        n = len(state.latencies)
        p95 = float(state.latencies[math.ceil(0.95 * n) - 1]) if n else None  # nearest rank
        return WindowStats(
            volume=state.lines,
            errors=state.errors,
            warns=state.warns,
            error_rate=state.errors / state.lines if state.lines else 0.0,
            p95_latency=p95,
            latency_samples=n,
            pattern_counts=dict(state.patterns),
            distinct_error_patterns=len(state.error_patterns),
        )

    def recent_volume(self, service: str, seconds: float) -> int:
        """Lines in the most recent ``seconds`` of sealed buckets (whole window if larger)."""
        state = self._states.get(service)
        if state is None:
            return 0
        n = max(1, round(seconds / self._settings.TICK_SECONDS))
        return sum(b.lines for b in islice(reversed(state.buckets), n))

    def first_seen(self, service: str, pattern_id: str) -> float | None:
        info = self._info(service, pattern_id)
        return info.first_seen if info else None

    def template(self, service: str, pattern_id: str) -> str | None:
        info = self._info(service, pattern_id)
        return info.template if info else None

    def expected_count(self, service: str, pattern_id: str) -> float:
        """Long-run expected count of this pattern per window (0 if unknown or brand new)."""
        info = self._info(service, pattern_id)
        return info.rate if info else 0.0

    def samples(
        self, service: str, n: int = 3, pattern_ids: Collection[str] | None = None
    ) -> list[str]:
        """The latest ``n`` redacted error lines, optionally only for the given patterns."""
        state = self._states.get(service)
        if state is None:
            return []
        lines = [m for pid, m in state.samples if pattern_ids is None or pid in pattern_ids]
        return lines[-n:]

    def _info(self, service: str, pattern_id: str) -> _PatternInfo | None:
        state = self._states.get(service)
        return state.info.get(pattern_id) if state else None
