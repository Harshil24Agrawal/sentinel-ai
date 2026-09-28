"""Wires the stages together and runs the tick loop.

collector -> parser (redacts) -> aggregator -> detectors -> incident manager -> hub (WebSocket),
plus the naive comparison detector and the SQLite audit trail. One asyncio process; every stage
is guarded so a failure is counted and logged, never fatal.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, deque
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from app.aggregator import Aggregator
from app.baseline import Baseline
from app.clock import Clock, SystemClock
from app.collector import LogCollector
from app.config import Settings, get_settings
from app.correlation import DependencyGraph
from app.detectors import Detectors, Evaluation
from app.explainer.base import Explainer
from app.incidents import IncidentManager
from app.models import (
    IncidentEvent,
    IncidentState,
    NotifyRequest,
    ServiceBandView,
    ServiceHealth,
    ServiceStatus,
    Stats,
)
from app.naive import NaiveDetector
from app.parser import LogParser
from app.redaction import Redactor
from app.store import Store
from generator.generator import LogGenerator
from generator.scenarios import RECOVER
from generator.services import ServiceSpec, load_services

logger = logging.getLogger(__name__)
T = TypeVar("T")

MAX_LINES_PER_TICK = 200_000  # a safety valve if the collector ever floods the queue


class SimulationDisabledError(RuntimeError):
    """/api/simulate was called but the built-in generator is switched off."""


class Hub:
    """Fan-out of WebSocket messages. Each subscriber has a small bounded queue and a slow one
    loses its OLDEST messages: a stuck browser tab must never slow the pipeline down."""

    def __init__(self, queue_size: int = 256) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)


class Pipeline:
    def __init__(
        self,
        clock: Clock,
        settings: Settings,
        *,
        services: dict[str, ServiceSpec],
        store: Store | None = None,
        generator: LogGenerator | None = None,
        explainer: Explainer | None = None,
    ) -> None:
        self.clock = clock
        self.settings = settings
        self.services = services
        self.generator = generator
        self.store = store
        self.started_at = clock.now()

        self.redactor = Redactor()  # its counter is the dashboard's "PHI fields redacted"
        self.parser = LogParser(clock, settings=settings, redactor=self.redactor)
        self.aggregator = Aggregator(clock, settings)
        self.baseline = Baseline(clock, settings)
        self.detectors = Detectors(self.aggregator, self.baseline, settings)
        self.graph = DependencyGraph.from_services(services)
        self.manager = IncidentManager(
            clock,
            self.aggregator,
            self.baseline,
            self.graph,
            settings=settings,
            store=store,
            explainer=explainer,
        )
        self.naive = NaiveDetector(clock, self.aggregator, settings)

        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=MAX_LINES_PER_TICK)
        self.collector = LogCollector(
            settings.LOG_PATH,
            self.queue,
            from_start=settings.COLLECTOR_FROM_START,
            poll_seconds=settings.COLLECTOR_POLL_SECONDS,
        )
        self.hub = Hub()
        self.history: deque[dict[str, Any]] = deque(
            maxlen=max(1, round(settings.HISTORY_SECONDS / settings.TICK_SECONDS))
        )
        self.errors: Counter[str] = Counter()
        self.ticks = 0
        self.scenario_runs: list[tuple[str, float]] = []
        self.notifications: list[dict[str, Any]] = []  # dry-run log until Phase 6's notifier
        self._evaluations: dict[str, Evaluation] = {}
        self._announced: set[str] = set()  # groups the UI has been told about
        self._tasks: list[asyncio.Task[None]] = []

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, clock: Clock | None = None
    ) -> Pipeline:
        settings = settings or get_settings()
        clock = clock or SystemClock()
        services = load_services()
        generator = None
        if settings.GENERATOR_ENABLED:
            generator = LogGenerator(
                clock,
                settings.LOG_PATH,
                services=services,
                seed=settings.GENERATOR_SEED,
                tick_seconds=settings.TICK_SECONDS,
            )
        return cls(
            clock, settings, services=services, store=Store(settings.DB_PATH), generator=generator
        )

    # ---- lifecycle --------------------------------------------------------------------

    def start(self) -> None:
        """Start the collector, the tick loop and (if enabled) the generator as tasks."""
        self._tasks = [
            asyncio.create_task(self.collector.run(), name="collector"),
            asyncio.create_task(self.run(), name="pipeline"),
        ]
        if self.generator is not None:
            self._tasks.append(asyncio.create_task(self.generator.run(), name="generator"))

    async def stop(self) -> None:
        """Cancel the tasks, wait for them, and close the store."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.store is not None:
            self.store.close()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        tick = self.settings.TICK_SECONDS
        next_at = loop.time()
        while True:
            try:
                self.tick_once()
            except Exception as exc:  # a tick must never kill the loop
                self.errors["tick"] += 1
                logger.error("tick failed", extra={"error": type(exc).__name__})
            next_at += tick
            now = loop.time()
            if next_at < now - tick:  # far behind: skip ahead instead of bursting to catch up
                next_at = now
            await asyncio.sleep(max(0.0, next_at - now))

    # ---- one tick ---------------------------------------------------------------------

    def _stage(self, name: str, fn: Callable[[], T], default: T) -> T:
        try:
            return fn()
        except Exception as exc:
            self.errors[name] += 1
            logger.error(
                "pipeline stage failed", extra={"stage": name, "error": type(exc).__name__}
            )
            return default

    def _drain(self) -> list[str]:
        lines: list[str] = []
        while len(lines) < MAX_LINES_PER_TICK:
            try:
                lines.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return lines

    def _ingest(self, line: str) -> None:
        try:
            self.aggregator.add(self.parser.parse(line))
        except Exception as exc:  # one bad line never stops monitoring
            self.errors["ingest"] += 1
            logger.error("line ingest failed", extra={"error": type(exc).__name__})

    def tick_once(self, lines: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Run one tick: ingest ``lines`` (default: whatever the collector queued), advance the
        window, detect, update incidents and publish. Returns the messages that were published.
        Tests drive this directly with a FakeClock."""
        for line in self._drain() if lines is None else lines:
            self._ingest(line)

        frozen = self.manager.open_services()
        self._stage("aggregate", lambda: self.aggregator.tick(frozen), None)
        self._evaluations = self._stage("detect", lambda: self.detectors.run(frozen), {})
        events = self._stage("incidents", lambda: self.manager.process(self._evaluations), [])
        self._stage("naive", self.naive.tick, 0)

        messages: list[dict[str, Any]] = []
        metrics = self._stage("metrics", self._metrics_frame, None)
        if metrics is not None:
            self.history.append(metrics)
            messages.append({"type": "metrics", "data": metrics})
        messages.extend(self._stage("events", lambda: self._event_messages(events), []))
        for message in messages:
            self.hub.publish(message)
        self.ticks += 1
        return messages

    # ---- views ------------------------------------------------------------------------

    def _service_names(self) -> list[str]:
        known = list(self.services)
        return known + [s for s in self.aggregator.services() if s not in self.services]

    def _status(self, service: str) -> ServiceHealth:
        if not self.baseline.ready(service):
            return "warming_up"
        severity, silent = self.manager.service_status(service)
        if silent:
            return "silent"
        return severity.name if severity is not None else "healthy"  # type: ignore[return-value]

    def _metrics_frame(self) -> dict[str, Any]:
        services: dict[str, Any] = {}
        for name in self._service_names():
            window = self.aggregator.window(name)
            services[name] = ServiceBandView(
                volume=window.volume,
                error_rate=window.error_rate,
                p95_latency=window.p95_latency,
                band=self.baseline.error_rate_band(name),
                status=self._status(name),
            ).model_dump(mode="json")
        return {"t": self.clock.now(), "services": services}

    def latest_metrics(self) -> dict[str, Any] | None:
        return self.history[-1] if self.history else None

    def metrics_history(self, seconds: float | None = None) -> list[dict[str, Any]]:
        frames = list(self.history)
        if seconds is not None and frames:
            cutoff = frames[-1]["t"] - seconds
            frames = [f for f in frames if f["t"] > cutoff]
        return frames

    def services_view(self) -> list[ServiceStatus]:
        out = []
        for name in self._service_names():
            window = self.aggregator.window(name)
            spec = self.services.get(name)
            out.append(
                ServiceStatus(
                    name=name,
                    status=self._status(name),
                    volume=window.volume,
                    error_rate=window.error_rate,
                    p95_latency=window.p95_latency,
                    baseline=self.baseline.view(name),
                    depends_on=list(spec.depends_on) if spec else [],
                )
            )
        return out

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "uptime_s": round(self.clock.now() - self.started_at, 1),
            "aws": "live" if self.settings.AWS_ENABLED else "dryrun",
            "llm": self.settings.LLM_PROVIDER,
        }

    def stats(self) -> Stats:
        return Stats(
            naive_alerts=self.naive.alerts,
            sentinel_incidents=self.manager.groups_opened,
            sentinel_notifications=self.manager.notifications_emitted,
            detection_latency_s=self._detection_latency(),
            phi_redacted=self.redactor.total,
        )

    def _detection_latency(self) -> dict[str, float]:
        """Seconds from a scenario starting to the first incident opening that follows it (and
        before the next scenario starts). Scenarios that opened nothing are left out."""
        opened = sorted(i.opened_at for i in self.manager.incidents())
        latency: dict[str, float] = {}
        for index, (name, start) in enumerate(self.scenario_runs):
            if name == RECOVER:
                continue
            end = (
                self.scenario_runs[index + 1][1]
                if index + 1 < len(self.scenario_runs)
                else float("inf")
            )
            first = next((t for t in opened if start <= t < end), None)
            if first is not None:
                latency[name] = round(first - start, 1)
            else:
                latency.pop(name, None)  # a later run of the same scenario found nothing
        return latency

    # ---- controls ---------------------------------------------------------------------

    def simulate(self, scenario: str) -> float:
        """Start a fault scenario in the built-in generator. Returns its start time.
        Raises SimulationDisabledError, or generator.scenarios.UnknownScenarioError."""
        if self.generator is None:
            raise SimulationDisabledError("the built-in generator is disabled")
        started = self.generator.start_scenario(scenario)
        self.scenario_runs.append((scenario, started))
        return started

    def set_sensitivity(self, open_z: float) -> None:
        """The dashboard's sensitivity slider. Detectors read the setting on every tick."""
        self.settings.OPEN_Z = open_z

    # ---- outgoing messages ------------------------------------------------------------

    def _event_messages(self, events: list[IncidentEvent]) -> list[dict[str, Any]]:
        """Turn per-service incident events into ONE message per affected group."""
        by_group: dict[str, list[IncidentEvent]] = {}
        for event in events:
            by_group.setdefault(event.incident.group_id, []).append(event)

        messages: list[dict[str, Any]] = []
        for group_id, group_events in by_group.items():
            summary = self.manager.group_summary(group_id)
            if summary is None:  # merged into another group this tick
                continue
            reopened = any(e.transition == "reopened" for e in group_events)
            if group_id not in self._announced or reopened:
                kind = "incident_opened"
                self._announced.add(group_id)
            elif summary.state is IncidentState.RESOLVED:
                kind = "incident_resolved"
            else:
                kind = "incident_updated"
            messages.append({"type": kind, "data": summary.model_dump(mode="json")})
            for event in group_events:
                if event.notify is not None:
                    messages.append(self._notification_message(event.notify, group_id))
        return messages

    def _notification_message(self, request: NotifyRequest, group_id: str) -> dict[str, Any]:
        """Placeholder until Phase 6: every notification is a dry run (nothing is sent to AWS)."""
        self.notifications.append(
            {
                "ts": self.clock.now(),
                "channel": "dryrun",
                "kind": request.kind,
                "group_id": group_id,
                "severity": request.severity.name,
                "subject": request.subject,
                "body": request.body,
            }
        )
        return {
            "type": "notification",
            "data": {
                "channel": "dryrun",
                "incident_id": group_id,
                "severity": request.severity.name,
                "ok": True,
            },
        }
