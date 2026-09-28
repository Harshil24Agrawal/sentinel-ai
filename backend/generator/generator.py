"""Synthetic log generator: writes realistic traffic and injected faults to the log file."""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.clock import Clock
from generator import messages
from generator.scenarios import (
    RECOVER,
    SCENARIO_NAMES,
    Effect,
    ScenarioPlan,
    UnknownScenarioError,
    build_plan,
)
from generator.services import ServiceSpec, load_services

LATENCY_SHARE = 0.9  # share of lines that carry latency_ms


@dataclass(slots=True)
class _Run:
    plan: ScenarioPlan
    started_at: float


_second_prefix: dict[int, str] = {}  # whole second -> "YYYY-MM-DDTHH:MM:SS." (a line-rate cache)


def format_ts(epoch: float) -> str:
    whole = int(epoch)
    prefix = _second_prefix.get(whole)
    if prefix is None:
        if len(_second_prefix) > 4096:
            _second_prefix.clear()
        prefix = _second_prefix[whole] = datetime.fromtimestamp(whole, UTC).strftime(
            "%Y-%m-%dT%H:%M:%S."
        )
    return f"{prefix}{int((epoch - whole) * 1000):03d}Z"


class LogGenerator:
    """Emits one second of traffic per ``tick`` and appends it to ``log_path``.

    All time comes from the injected clock. The file is the only channel to the collector:
    nothing is short-circuited in memory.
    """

    def __init__(
        self,
        clock: Clock,
        log_path: str | Path,
        *,
        services: dict[str, ServiceSpec] | None = None,
        seed: int | None = None,
        tick_seconds: float = 1.0,
    ) -> None:
        self._clock = clock
        self._path = Path(log_path)
        self.services = services if services is not None else load_services()
        self._rng = random.Random(seed)
        self.tick_seconds = tick_seconds
        self._runs: list[_Run] = []
        # For detection-latency measurement: when each service's fault began (absolute epoch).
        self.fault_injected_at: dict[str, float] = {}
        self.scenario_started_at: dict[str, float] = {}
        self.scenario_origin: dict[str, str] = {}

    # ---- scenario control -------------------------------------------------------------

    def start_scenario(self, name: str, service: str | None = None) -> float:
        """Start a scenario (or ``recover``). Returns the start time (epoch seconds)."""
        now = self._clock.now()
        if name == RECOVER:
            self.recover()
            return now
        plan = build_plan(name, self.services, service)
        self._runs.append(_Run(plan, now))
        self.scenario_started_at[name] = now
        self.scenario_origin[name] = plan.origin
        for svc, offset in plan.onsets.items():
            self.fault_injected_at[svc] = now + offset
        return now

    def recover(self) -> None:
        """All services return to normal immediately; detectors then take their hysteresis."""
        self._runs.clear()

    def active_scenarios(self) -> list[str]:
        self._prune(self._clock.now())
        return [r.plan.name for r in self._runs]

    @staticmethod
    def scenario_names() -> tuple[str, ...]:
        return SCENARIO_NAMES

    # ---- generation -------------------------------------------------------------------

    def _prune(self, now: float) -> None:
        self._runs = [
            r for r in self._runs if r.plan.duration is None or now - r.started_at < r.plan.duration
        ]

    def _effect(self, spec: ServiceSpec, now: float) -> Effect:
        merged: Effect | None = None
        for run in self._runs:
            effect = run.plan.effect(spec, now - run.started_at)
            if effect is not None:
                merged = effect if merged is None else merged.merge(effect)
        return merged or Effect()

    def _line(self, spec: ServiceSpec, ts: float, eff: Effect) -> str:
        rng = self._rng
        msgs = messages.MESSAGES[spec.name]
        p_error = eff.error_rate if eff.error_rate is not None else spec.base_error
        latency_mean = spec.latency_mean_ms * eff.latency_mult
        latency_std = spec.latency_std_ms * eff.latency_mult

        if rng.random() < p_error:
            level = "ERROR"
            if eff.messages and rng.random() < eff.message_share:
                message = messages.render(rng.choice(eff.messages), rng)
                if eff.error_latency_ms is not None:
                    latency_mean, latency_std = eff.error_latency_ms
            else:
                message = messages.render(msgs.error, rng)
        else:
            roll = rng.random()
            if roll < messages.WARN_RATE:
                level, message = "WARN", messages.render(msgs.warn, rng)
            else:
                level = "DEBUG" if roll < messages.WARN_RATE + messages.DEBUG_RATE else "INFO"
                message = messages.render(rng.choice(msgs.info), rng)
                if rng.random() < messages.PHI_LINE_RATE:
                    message = f"{message} {messages.phi_suffix(rng)}"

        prefix = f"{format_ts(ts)} {level:<5} {spec.name}"
        if rng.random() < LATENCY_SHARE:
            latency = max(1, int(rng.gauss(latency_mean, latency_std)))
            return f"{prefix} latency_ms={latency} {message}"
        return f"{prefix} {message}"

    def generate(self) -> list[str]:
        """One tick of traffic, timestamped within the last ``tick_seconds``, time-ordered."""
        now = self._clock.now()
        self._prune(now)
        stamped: list[tuple[float, str]] = []
        for spec in self.services.values():
            eff = self._effect(spec, now)
            if eff.silent:
                continue
            count = max(0, round(self._rng.gauss(spec.rps, math.sqrt(spec.rps))))
            for _ in range(count):
                ts = now - self._rng.random() * self.tick_seconds
                stamped.append((ts, self._line(spec, ts, eff)))
        stamped.sort(key=lambda item: item[0])
        return [line for _, line in stamped]

    def write(self, lines: list[str]) -> None:
        if not lines:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")

    def tick(self) -> int:
        """Generate one tick and append it to the log file. Returns the line count."""
        lines = self.generate()
        self.write(lines)
        return len(lines)

    async def run(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(self.tick_seconds)


__all__ = ["LogGenerator", "UnknownScenarioError", "format_ts"]
