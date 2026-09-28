"""Test rig: synthetic traffic pushed through aggregator -> detectors on a FakeClock."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from app.aggregator import Aggregator
from app.baseline import Baseline
from app.clock import FakeClock
from app.config import Settings
from app.detectors import Detectors, Evaluation
from app.models import Level, LogEvent, Signal, SignalType
from app.parser import LogParser
from app.patterns import mine
from app.redaction import Redactor
from generator.generator import LogGenerator

START = 1_000_000.0
WARM = 100  # ticks after which a default-config service is ready (window 60 + 30 samples)


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def make_event(
    service: str,
    message: str,
    *,
    error: bool = False,
    warn: bool = False,
    latency: float | None = None,
    ts: float = START,
) -> LogEvent:
    pattern_id = template = None
    if error or warn:
        pattern = mine(message)
        pattern_id, template = pattern.id, pattern.template
    return LogEvent(
        ts=ts,
        level=Level.ERROR if error else Level.WARN if warn else Level.INFO,
        is_error=error,
        is_warn=warn,
        service=service,
        latency_ms=None if latency is None else int(latency),
        message=message,
        raw=message,
        pattern_id=pattern_id,
        template=template,
    )


@dataclass
class Feed:
    """Steady synthetic traffic for one service. Fields can be changed mid-run."""

    service: str
    lines: int = 20  # lines per emitting second
    every: int = 1  # emit only every Nth second (for low-volume services)
    p_error: float = 0.02
    latency: tuple[float, float] | None = (100.0, 10.0)
    message: str = "Request {n} completed"
    error_message: str = "Request {n} failed"
    noisy: bool = True  # random errors; False spreads them evenly (no noise at all)
    start: int = 0
    stop: int | None = None  # exclusive
    _acc: float = 0.0


class Rig:
    """Does per tick exactly what the pipeline will: add lines, tick, detect, advance."""

    def __init__(self, *feeds: Feed, **settings: Any) -> None:
        self.settings = make_settings(**settings)
        self.clock = FakeClock(START)
        self.agg = Aggregator(self.clock, self.settings)
        self.baseline = Baseline(self.clock, self.settings)
        self.detectors = Detectors(self.agg, self.baseline, self.settings)
        self.feeds = list(feeds)
        self.frozen: set[str] = set()
        self.rng = random.Random(1)
        self.t = 0
        self.history: list[dict[str, Evaluation]] = []

    def _emit(self, feed: Feed) -> None:
        if self.t < feed.start or (feed.stop is not None and self.t >= feed.stop):
            return
        if (self.t - feed.start) % feed.every:
            return
        if feed.noisy:
            errors = sum(self.rng.random() < feed.p_error for _ in range(feed.lines))
        else:
            feed._acc += feed.p_error * feed.lines
            errors = int(feed._acc)
            feed._acc -= errors
        for i in range(feed.lines):
            latency = None
            if feed.latency is not None:
                latency = max(1.0, self.rng.gauss(*feed.latency))
            is_error = i < errors
            template = feed.error_message if is_error else feed.message
            message = template.format(n=self.rng.randint(10000, 99999))
            self.agg.add(make_event(feed.service, message, error=is_error, latency=latency))

    def _produce(self) -> None:
        """Push this second's lines into the aggregator (subclasses swap the traffic source)."""
        for feed in self.feeds:
            self._emit(feed)

    def tick(self) -> dict[str, Evaluation]:
        self._produce()
        self.agg.tick(self.frozen)
        result = self.detectors.run(self.frozen)
        self.clock.advance(1)
        self.t += 1
        self.history.append(result)
        return result

    def run(self, seconds: int) -> list[dict[str, Evaluation]]:
        return [self.tick() for _ in range(seconds)]

    def signals(
        self, service: str, kind: SignalType | None = None, since: int = 0
    ) -> list[tuple[int, Signal]]:
        """(tick, signal) pairs for a service from tick ``since`` on."""
        return [
            (i, sig)
            for i, result in enumerate(self.history)
            if i >= since and service in result
            for sig in result[service].signals
            if kind is None or sig.type is kind
        ]

    def first_signal_tick(
        self, service: str, kind: SignalType | None = None, since: int = 0
    ) -> int | None:
        found = self.signals(service, kind, since)
        return found[0][0] if found else None


class GenRig(Rig):
    """The same rig fed by the real generator, through the real parser (no file needed:
    file round trips are covered by the collector tests)."""

    def __init__(self, seed: int = 7, **settings: Any) -> None:
        super().__init__(**settings)
        self.gen = LogGenerator(self.clock, "unused.log", seed=seed)  # generate() never writes
        self.parser = LogParser(self.clock, settings=self.settings, redactor=Redactor())

    def _produce(self) -> None:
        for line in self.gen.generate():
            self.agg.add(self.parser.parse(line))
