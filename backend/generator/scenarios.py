"""Fault scenarios (CLAUDE.md §5). Each plan says how a service misbehaves t seconds in."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from generator.services import ServiceSpec

RECOVER = "recover"

# Templates for fault-specific errors ({id}, {ms}, {hex}, {auth_code} are filled by messages.render)
POOL_EXHAUSTED = "Connection pool exhausted after {ms} ms"
DB_TIMEOUT_APPOINTMENT = "Database timeout while saving appointment {id}"
DB_TIMEOUT_CLAIM = "Database timeout while saving claim {id}"
NOTIFICATION_TIMEOUT = "Delivery callback timeout for notification {id} after {ms} ms"
TOKEN_INVALID = "Token validation failed: signing key {hex} not found"
SESSION_REJECTED = (
    "Session validation failed: auth-service returned HTTP {auth_code} for request {id}"
)


@dataclass(frozen=True, slots=True)
class Effect:
    """How one service behaves right now, relative to normal."""

    error_rate: float | None = None  # replaces the service's base error rate when set
    latency_mult: float = 1.0
    silent: bool = False
    messages: tuple[str, ...] = ()  # fault-specific error templates
    message_share: float = 1.0  # share of errors that use them (the rest use the base error)
    error_latency_ms: tuple[float, float] | None = None  # (mean, std) for fault errors

    def merge(self, other: Effect) -> Effect:
        """Combine effects from concurrently running scenarios."""
        rates = [r for r in (self.error_rate, other.error_rate) if r is not None]
        if not self.messages:
            share = other.message_share
        elif not other.messages:
            share = self.message_share
        else:
            share = max(self.message_share, other.message_share)
        return Effect(
            error_rate=max(rates) if rates else None,
            latency_mult=self.latency_mult * other.latency_mult,
            silent=self.silent or other.silent,
            messages=self.messages + other.messages,
            message_share=share,
            error_latency_ms=self.error_latency_ms or other.error_latency_ms,
        )


EffectFn = Callable[[ServiceSpec, float], Effect | None]


@dataclass(frozen=True)
class ScenarioPlan:
    name: str
    origin: str  # service where the fault is injected first
    onsets: dict[str, float]  # service -> seconds after start when its own fault begins
    effect: EffectFn
    duration: float | None = None  # None: runs until `recover`
    description: str = field(default="", compare=False)


def ramp(t: float, t0: float, t1: float, v0: float, v1: float) -> float:
    """Linear from v0 at t0 to v1 at t1, clamped outside."""
    if t <= t0:
        return v0
    if t >= t1:
        return v1
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def _db_degradation(spec: ServiceSpec, t: float) -> Effect | None:
    name = spec.name
    if name == "database":
        mult = ramp(t, 0, 15, 1, 20)
        if t < 15:
            return Effect(latency_mult=mult)
        return Effect(
            latency_mult=mult,
            error_rate=ramp(t, 15, 30, spec.base_error, 0.25),
            messages=(POOL_EXHAUSTED,),
            error_latency_ms=(3000, 200),
        )
    if name in ("appointment-service", "claims-service"):
        mult = ramp(t, 5, 20, 1, 3)  # slows down as the database does
        if t < 15:
            return Effect(latency_mult=mult)
        template = DB_TIMEOUT_CLAIM if name == "claims-service" else DB_TIMEOUT_APPOINTMENT
        return Effect(
            latency_mult=mult,
            error_rate=ramp(t, 15, 30, spec.base_error, 0.30),
            messages=(template,),
            error_latency_ms=(3000, 300),
        )
    if name == "notification-service" and t >= 35:
        return Effect(
            error_rate=ramp(t, 35, 50, spec.base_error, 0.20),
            messages=(NOTIFICATION_TIMEOUT,),
            error_latency_ms=(2500, 300),
        )
    return None


def _auth_outage(spec: ServiceSpec, t: float) -> Effect | None:
    if spec.name == "auth-service":
        return Effect(
            error_rate=ramp(t, 0, 10, spec.base_error, 0.40),
            messages=(TOKEN_INVALID,),
            message_share=0.6,  # the rest are the usual 401/403 lines, which then surge
        )
    if spec.name == "patient-portal" and t >= 15:
        return Effect(
            error_rate=ramp(t, 15, 30, spec.base_error, 0.15),
            messages=(SESSION_REJECTED,),
        )
    return None


def _silent(spec: ServiceSpec, t: float) -> Effect | None:
    return Effect(silent=True) if spec.name == "notification-service" else None


def build_plan(
    name: str, services: dict[str, ServiceSpec], service: str | None = None
) -> ScenarioPlan:
    """Build the plan for a scenario. ``service`` picks the target of ``noisy_blip``."""
    if name == "db_degradation":
        return ScenarioPlan(
            name=name,
            origin="database",
            onsets={
                "database": 0,
                "appointment-service": 15,
                "claims-service": 15,
                "notification-service": 35,
            },
            effect=_db_degradation,
            description="Database latency x20, pool exhaustion, downstream timeouts",
        )
    if name == "auth_outage":
        return ScenarioPlan(
            name=name,
            origin="auth-service",
            onsets={"auth-service": 0, "patient-portal": 15},
            effect=_auth_outage,
            description="auth-service rejects tokens; patient-portal errors follow",
        )
    if name == "silent_service":
        return ScenarioPlan(
            name=name,
            origin="notification-service",
            onsets={"notification-service": 0},
            effect=_silent,
            description="notification-service stops logging",
        )
    if name == "noisy_blip":
        target = service or "claims-service"
        if target not in services:
            raise ValueError(f"unknown service {target!r}")
        return ScenarioPlan(
            name=name,
            origin=target,
            onsets={target: 0},
            effect=lambda spec, t: Effect(error_rate=0.25) if spec.name == target else None,
            duration=3.0,
            description=f"{target} spikes to 25% errors for 3 seconds",
        )
    raise UnknownScenarioError(name)


class UnknownScenarioError(KeyError):
    """Raised for a scenario name that does not exist."""


SCENARIO_NAMES: tuple[str, ...] = (
    "db_degradation",
    "auth_outage",
    "silent_service",
    "noisy_blip",
    RECOVER,
)
