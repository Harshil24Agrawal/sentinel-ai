"""Test rig: synthetic traffic pushed through aggregator -> detectors on a FakeClock."""

from __future__ import annotations

import copy
import dataclasses
import random
from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from app.aggregator import Aggregator
from app.baseline import Baseline
from app.clock import FakeClock
from app.config import Settings
from app.correlation import DependencyGraph
from app.detectors import Detectors, Evaluation
from app.incidents import IncidentManager
from app.models import Incident, IncidentEvent, Level, LogEvent, NotifyRequest, Signal, SignalType
from app.naive import NaiveDetector
from app.parser import LogParser
from app.patterns import mine
from app.pipeline import Pipeline
from app.redaction import Redactor
from app.store import Store
from generator.generator import LogGenerator, format_ts
from generator.services import load_services

START = 1_000_000.0
WARM = 100  # ticks after which a default-config service is ready (window 60 + 30 samples)
WARMED = 150  # ticks of normal traffic before a scenario starts in the shared pipelines


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

    def __init__(self, seed: int = 7, rps_scale: float = 1.0, **settings: Any) -> None:
        super().__init__(**settings)
        services = {
            name: dataclasses.replace(spec, rps=spec.rps * rps_scale)
            for name, spec in load_services().items()
        }
        self.gen = LogGenerator(self.clock, "unused.log", services=services, seed=seed)
        self.parser = LogParser(self.clock, settings=self.settings, redactor=Redactor())

    def _produce(self) -> None:
        for line in self.gen.generate():
            self.agg.add(self.parser.parse(line))


# ---- Phase 3: incident manager ---------------------------------------------------------


def evaluation(
    service: str,
    z: float = 0.0,
    *,
    open_z: float = 3.0,
    extra: tuple[Signal, ...] = (),
    ready: bool = True,
) -> Evaluation:
    """A synthetic per-tick evaluation. ``z`` is the error-rate z: at or above ``open_z`` the
    error_rate detector fires (with a big absolute rise); below it, nothing fires but the
    measurement is still there, exactly like the real detectors."""
    fires = z >= open_z
    signals = list(extra)
    if fires:
        signals.append(
            Signal(
                type=SignalType.ERROR_RATE,
                service=service,
                value=0.30,
                baseline_median=0.02,
                z=z,
                detail=f"error rate 30.0% vs normal 2.0% (z={z:.1f})",
            )
        )
    return Evaluation(
        service,
        ready,
        signals,
        error_z=z,
        error_delta=0.28 if fires else 0.0,
        latency_z=0.0,
        shortfall_z=0.0,
    )


def pattern_signal(service: str, kind: SignalType = SignalType.NEW_PATTERN) -> Signal:
    return Signal(
        type=kind,
        service=service,
        value=5.0,
        baseline_median=0.0,
        z=0.0,
        detail=f"{kind.value} x5: Something odd <NUM>",
        pattern_id="abc12345",
    )


class Harness:
    """The incident manager on its own, fed hand-made evaluations (no traffic)."""

    def __init__(self, graph: DependencyGraph | None = None, store: bool = False, **settings: Any):
        self.settings = make_settings(**settings)
        self.clock = FakeClock(START)
        self.agg = Aggregator(self.clock, self.settings)
        self.baseline = Baseline(self.clock, self.settings)
        self.store = Store(":memory:") if store else None
        self.graph = graph if graph is not None else DependencyGraph({})
        self.manager = IncidentManager(
            self.clock,
            self.agg,
            self.baseline,
            self.graph,
            settings=self.settings,
            store=self.store,
        )
        self.t = 0
        self.events: list[tuple[int, IncidentEvent]] = []

    def tick(self, zs: Mapping[str, float | Evaluation] | None = None) -> list[IncidentEvent]:
        evals = {
            name: (
                v if isinstance(v, Evaluation) else evaluation(name, v, open_z=self.settings.OPEN_Z)
            )
            for name, v in (zs or {}).items()
        }
        events = self.manager.process(evals)
        self.events.extend((self.t, e) for e in events)
        self.clock.advance(1)
        self.t += 1
        return events

    def run(
        self, n: int, zs: Mapping[str, float | Evaluation] | None = None
    ) -> list[IncidentEvent]:
        return [e for _ in range(n) for e in self.tick(zs)]

    def incident(self, service: str) -> Incident:
        (found,) = [i for i in self.manager.incidents() if i.service == service]
        return found

    def notifies(self) -> list[NotifyRequest]:
        return [e.notify for _, e in self.events if e.notify is not None]


def services_graph() -> DependencyGraph:
    return DependencyGraph.from_services(load_services())


# ---- Phase 3 on the real pipeline ------------------------------------------------------


class _Managed:
    """Adds the Phase 3 stages to a rig: incident manager (driving the baseline freeze), naive
    detector, and optionally an in-memory store."""

    def __init__(
        self, *args: Any, graph: DependencyGraph | None = None, with_store: bool = False, **kw: Any
    ) -> None:
        super().__init__(*args, **kw)  # type: ignore[misc]
        if graph is None:
            gen = getattr(self, "gen", None)
            graph = DependencyGraph.from_services(gen.services) if gen else DependencyGraph({})
        self.graph = graph
        self.store = Store(":memory:") if with_store else None  # not deep-copyable: opt in
        self.manager = IncidentManager(
            self.clock,  # type: ignore[attr-defined]
            self.agg,  # type: ignore[attr-defined]
            self.baseline,  # type: ignore[attr-defined]
            graph,
            settings=self.settings,  # type: ignore[attr-defined]
            store=self.store,
        )
        self.naive = NaiveDetector(self.clock, self.agg, self.settings)  # type: ignore[attr-defined]
        self.events: list[tuple[int, IncidentEvent]] = []

    def tick(self) -> dict[str, Evaluation]:
        self._produce()  # type: ignore[attr-defined]
        frozen = self.manager.open_services()
        self.agg.tick(frozen)  # type: ignore[attr-defined]
        result = self.detectors.run(frozen)  # type: ignore[attr-defined]
        self.events.extend((self.t, e) for e in self.manager.process(result))  # type: ignore[attr-defined]
        self.naive.tick()
        self.clock.advance(1)  # type: ignore[attr-defined]
        self.t += 1  # type: ignore[attr-defined]
        self.history.append(result)  # type: ignore[attr-defined]
        return result

    def notifies(self) -> list[NotifyRequest]:
        return [e.notify for _, e in self.events if e.notify is not None]


class ManagedRig(_Managed, Rig):
    pass


class ManagedGenRig(_Managed, GenRig):
    pass


def run_until_resolved(rig: ManagedGenRig, limit: int = 260) -> int:
    """Tick until every incident is resolved. Returns how many ticks that took."""
    from app.models import IncidentState

    for n in range(1, limit + 1):
        rig.tick()
        incidents = rig.manager.incidents()
        if incidents and all(i.state is IncidentState.RESOLVED for i in incidents):
            return n
    raise AssertionError(f"incidents still open after {limit}s: {rig.manager.incidents('open')}")


def outage(warmed: ManagedGenRig, scenario: str, seconds: int) -> SimpleNamespace:
    """Run a scenario on a copy of the warmed pipeline, snapshot it mid-outage, then recover
    and run until every incident has resolved."""
    rig = copy.deepcopy(warmed)
    started = rig.gen.start_scenario(scenario)
    rig.run(seconds)
    mid_tick = len(rig.history) - 1  # the last tick of the outage itself
    mid = rig.manager.incidents("open")
    mid_notifies = list(rig.notifies())
    baseline_samples = {svc: rig.baseline.samples(svc) for svc in rig.agg.services()}
    rig.gen.start_scenario("recover")
    recovered_at = rig.clock.now()
    ticks = run_until_resolved(rig)
    return SimpleNamespace(
        rig=rig,
        mid=mid,
        mid_tick=mid_tick,
        mid_notifies=mid_notifies,
        started=started,
        recovered_at=recovered_at,
        resolve_ticks=ticks,
        baseline_samples=baseline_samples,
    )


# ---- Phase 4: the whole Pipeline on synthetic log lines --------------------------------


class LineRig:
    """Feeds (see ``Feed``) rendered as real log lines and pushed through a real ``Pipeline``
    on a FakeClock. ``pipeline.tick_once(lines)`` is exactly what the async loop calls."""

    def __init__(
        self,
        *feeds: Feed,
        generator: LogGenerator | None = None,
        store: bool = True,
        **settings: Any,
    ) -> None:
        self.settings = make_settings(**settings)
        self.clock = FakeClock(START)
        self.pipeline = Pipeline(
            self.clock,
            self.settings,
            services=load_services(),
            store=Store(":memory:") if store else None,
            generator=generator,
        )
        self.feeds = list(feeds)
        self.rng = random.Random(1)
        self.t = 0
        self.messages: list[tuple[int, dict[str, Any]]] = []

    def _lines(self, feed: Feed) -> list[str]:
        if self.t < feed.start or (feed.stop is not None and self.t >= feed.stop):
            return []
        if (self.t - feed.start) % feed.every:
            return []
        if feed.noisy:
            errors = sum(self.rng.random() < feed.p_error for _ in range(feed.lines))
        else:
            feed._acc += feed.p_error * feed.lines
            errors = int(feed._acc)
            feed._acc -= errors
        now = self.clock.now()
        lines = []
        for i in range(feed.lines):
            is_error = i < errors
            template = feed.error_message if is_error else feed.message
            text = template.format(n=self.rng.randint(10000, 99999))
            latency = ""
            if feed.latency is not None:
                latency = f"latency_ms={max(1, int(self.rng.gauss(*feed.latency)))} "
            level = "ERROR" if is_error else "INFO"
            lines.append(
                f"{format_ts(now - self.rng.random())} {level:<5} {feed.service} {latency}{text}"
            )
        return lines

    def tick(self, extra_lines: list[str] | None = None) -> list[dict[str, Any]]:
        lines = [line for feed in self.feeds for line in self._lines(feed)]
        messages = self.pipeline.tick_once([*lines, *(extra_lines or [])])
        self.messages.extend((self.t, m) for m in messages)
        self.clock.advance(1)
        self.t += 1
        return messages

    def run(self, n: int) -> None:
        for _ in range(n):
            self.tick()

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [m["data"] for _, m in self.messages if m["type"] == kind]


def all_services_feeds(lines: int = 12, p_error: float = 0.02) -> list[Feed]:
    """Steady traffic for every service in services.yaml (latency ~100ms, ~2% errors)."""
    return [Feed(name, lines=lines, p_error=p_error) for name in load_services()]
