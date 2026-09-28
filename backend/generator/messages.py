"""Message templates for normal traffic, and the PHI-like fields sprinkled into it."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

PHI_LINE_RATE = 1 / 200  # share of normal lines that carry a PHI-like field
WARN_RATE = 0.03  # share of non-error lines logged at WARN
DEBUG_RATE = 0.05  # share of non-error lines logged at DEBUG (uses the INFO templates)

_FIRST = ("Jane", "John", "Maria", "Wei", "Aisha", "Carlos", "Priya", "Tom")
_LAST = ("Doe", "Smith", "Garcia", "Chen", "Khan", "Rivera", "Patel", "Miller")


@dataclass(frozen=True, slots=True)
class ServiceMessages:
    """A service has ONE warn and ONE base-error template, so baseline patterns stay stable."""

    info: tuple[str, ...]
    warn: str
    error: str


MESSAGES: dict[str, ServiceMessages] = {
    "patient-portal": ServiceMessages(
        info=(
            "Portal request {id} completed",
            "Session {id} refreshed",
            "Appointment list rendered for session {id}",
        ),
        warn="Slow page render for session {id}",
        error="Request {id} failed with HTTP {http5}",
    ),
    "auth-service": ServiceMessages(
        info=(
            "Token issued for session {id}",
            "Login succeeded for request {id}",
            "Token refreshed for session {id}",
        ),
        warn="Rate limit approaching for client {id}",
        error="Login rejected with HTTP {auth_code} for request {id}",
    ),
    "appointment-service": ServiceMessages(
        info=(
            "Appointment request {id} completed",
            "Appointment {id} rescheduled",
            "Reminder queued for appointment {id}",
        ),
        warn="Slot conflict detected for appointment {id}",
        error="Appointment request {id} failed validation",
    ),
    "claims-service": ServiceMessages(
        info=(
            "Claim {id} adjudicated against {n} rules",
            "Claim batch {id} accepted with {n} claims",
            "Eligibility check completed for claim {id}",
        ),
        warn="Claim {id} flagged for manual review",
        error="Claim {id} rejected: schema validation failed",
    ),
    "provider-service": ServiceMessages(
        info=(
            "Provider lookup {id} completed",
            "Provider directory synced {n} records",
            "Credential check passed for provider {id}",
        ),
        warn="Provider record {id} is stale",
        error="Provider lookup {id} failed",
    ),
    "notification-service": ServiceMessages(
        info=(
            "Notification {id} sent",
            "Email queued for notification {id}",
            "SMS gateway acknowledged notification {id}",
        ),
        warn="Retry scheduled for notification {id}",
        error="Notification {id} delivery failed",
    ),
    "database": ServiceMessages(
        info=(
            "Query {id} completed",
            "Checkpoint completed in {n} ms",
            "Connection acquired from pool {n}",
        ),
        warn="Slow query {id} detected",
        error="Query {id} aborted: transaction conflict",
    ),
}


def _hex(rng: random.Random) -> str:
    """12 hex chars, always containing a letter (so it templates to <HEX>, never <NUM>)."""
    return "".join(rng.choices("0123456789abcdef", k=11)) + rng.choice("abcdef")


_FIELDS: dict[str, Callable[[random.Random], object]] = {
    "id": lambda rng: rng.randint(10000, 99999),
    "n": lambda rng: rng.randint(1, 500),
    "ms": lambda rng: rng.randint(2500, 3500),
    "hex": _hex,
    "http5": lambda rng: rng.choice((500, 502, 503)),
    "auth_code": lambda rng: rng.choice((401, 403)),
}


def render(template: str, rng: random.Random) -> str:
    """Fill ``{id}``, ``{n}``, ``{ms}``, ``{hex}``, ``{http5}``, ``{auth_code}`` placeholders
    (only the ones the template uses are drawn)."""
    values = {name: draw(rng) for name, draw in _FIELDS.items() if f"{{{name}}}" in template}
    return template.format(**values)


def phi_suffix(rng: random.Random) -> str:
    """A synthetic PHI-like field, so redaction is visibly exercised."""
    first, last = rng.choice(_FIRST), rng.choice(_LAST)
    kind = rng.randrange(4)
    if kind == 0:
        return f"member_id=MBR{rng.randint(1000000, 9999999)}"
    if kind == 1:
        # Area numbers 900-999 are never issued, so these can't be real SSNs.
        return f"ssn=9{rng.randint(0, 99):02d}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}"
    if kind == 2:
        return f"email={first.lower()}.{last.lower()}@example.com"
    return f'patient_name="{first} {last}"'
