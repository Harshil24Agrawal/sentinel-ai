"""Correlation groups over the dependency graph; probable origin and blast radius."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.models import BlastNode, BlastRadius, Severity

if TYPE_CHECKING:
    from generator.services import ServiceSpec


class DependencyGraph:
    """``depends_on`` edges between services, queried in both directions."""

    def __init__(self, depends_on: Mapping[str, Iterable[str]]) -> None:
        self._down: dict[str, set[str]] = {s: set(deps) for s, deps in depends_on.items()}
        self._up: dict[str, set[str]] = {s: set() for s in self._down}
        for service, deps in list(self._down.items()):  # copy: unknown deps add entries
            for dep in deps:
                self._up.setdefault(dep, set()).add(service)
                self._down.setdefault(dep, set())

    @classmethod
    def from_services(cls, specs: Mapping[str, ServiceSpec]) -> DependencyGraph:
        return cls({name: spec.depends_on for name, spec in specs.items()})

    @property
    def services(self) -> list[str]:
        return list(self._down)

    def adjacent(self, a: str, b: str) -> bool:
        """A direct dependency in either direction."""
        return b in self._down.get(a, ()) or a in self._down.get(b, ())

    def dependencies(self, service: str) -> set[str]:
        """Everything ``service`` transitively depends on (its upstream)."""
        return self._reach(service, self._down)

    def dependents(self, service: str) -> set[str]:
        """Everything that transitively depends on ``service`` (its blast radius)."""
        return self._reach(service, self._up)

    @staticmethod
    def _reach(start: str, edges: Mapping[str, set[str]]) -> set[str]:
        seen: set[str] = set()
        queue = deque(edges.get(start, ()))
        while queue:
            node = queue.popleft()
            if node not in seen and node != start:
                seen.add(node)
                queue.extend(edges.get(node, ()))
        return seen


@dataclass(slots=True)
class Member:
    incident_id: str
    service: str
    opened_at: float
    deviated_at: float  # when the service first started deviating (start of its streak)


@dataclass
class CorrelationGroup:
    id: str
    opened_at: float
    members: list[Member] = field(default_factory=list)
    resolved_at: float | None = None
    notified: bool = False  # currently announced (reset when the resolve is announced)
    notified_ever: bool = False  # was ever announced: what the card shows
    notified_severity: Severity | None = None

    @property
    def services(self) -> list[str]:
        return [m.service for m in self.members]

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None


class CorrelationEngine:
    """Decides which incidents belong to the same outage.

    An incident that opens on service S joins an open group if some member of that group
    opened within ``window_seconds`` and is DIRECTLY connected to S in the dependency graph
    (either direction). Chaining through members lets a cascade grow one group
    (database -> appointment -> notification) without pulling in unrelated services that merely
    share a healthy dependency. If S connects to several open groups they are merged.
    """

    def __init__(self, graph: DependencyGraph, window_seconds: float) -> None:
        self.graph = graph
        self.window_seconds = window_seconds
        self.groups: dict[str, CorrelationGroup] = {}
        self._seq = 0

    def open_groups(self) -> list[CorrelationGroup]:
        return [g for g in self.groups.values() if g.is_open]

    def group_of(self, incident_id: str) -> CorrelationGroup | None:
        return next(
            (
                g
                for g in self.groups.values()
                if any(m.incident_id == incident_id for m in g.members)
            ),
            None,
        )

    def join(self, member: Member, now: float) -> tuple[CorrelationGroup, list[str]]:
        """Place a newly opened incident. Returns its group and the ids of incidents that were
        moved into it from a merged group (their ``group_id`` must be updated)."""
        matches = [
            g
            for g in self.open_groups()
            if any(
                abs(now - m.opened_at) <= self.window_seconds
                and self.graph.adjacent(member.service, m.service)
                for m in g.members
            )
        ]
        if not matches:
            self._seq += 1
            group = CorrelationGroup(id=f"grp-{int(now)}-{self._seq}", opened_at=now)
            group.members.append(member)
            self.groups[group.id] = group
            return group, []

        target = min(matches, key=lambda g: g.opened_at)
        moved: list[str] = []
        for other in matches:
            if other is target:
                continue
            for m in other.members:
                target.members.append(m)
                moved.append(m.incident_id)
            target.notified = target.notified or other.notified
            target.notified_ever = target.notified_ever or other.notified_ever
            if other.notified_severity is not None:
                target.notified_severity = max(
                    target.notified_severity or other.notified_severity, other.notified_severity
                )
            del self.groups[other.id]
        target.members.append(member)
        return target, moved

    def origin(self, group: CorrelationGroup) -> str:
        """The probable origin: the member the most other members depend on (transitively),
        i.e. the most upstream one; ties go to the one that deviated first."""
        members = {m.service: m for m in group.members}

        def upstream_of(service: str) -> int:
            return len(self.graph.dependents(service) & members.keys())

        return min(
            members.values(), key=lambda m: (-upstream_of(m.service), m.deviated_at, m.service)
        ).service

    def blast_radius(self, origin: str, impacted: Iterable[str]) -> BlastRadius:
        """Every service, marked ``impacted`` (has an open incident in the group), ``at_risk``
        (transitively depends on the origin but is not affected yet) or ``healthy``."""
        impacted_set = set(impacted)
        downstream = self.graph.dependents(origin)
        nodes = []
        for service in self.graph.services:
            if service in impacted_set:
                status = "impacted"
            elif service in downstream:
                status = "at_risk"
            else:
                status = "healthy"
            nodes.append(BlastNode(service=service, status=status))
        return BlastRadius(origin=origin, nodes=nodes)
