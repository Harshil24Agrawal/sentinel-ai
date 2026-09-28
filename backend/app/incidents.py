"""Incident lifecycle: persistence, hysteresis, cooldown/reopen, grouping, notifications.

This is the alert-fatigue core. Detectors say what looks odd this tick; the manager decides
what becomes an incident, when it is really over, and who gets told (once, not per tick).
"""

from __future__ import annotations

import logging
import textwrap
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.aggregator import Aggregator
from app.baseline import Baseline
from app.clock import Clock
from app.config import Settings, get_settings
from app.correlation import CorrelationEngine, CorrelationGroup, DependencyGraph, Member
from app.detectors import Evaluation
from app.explainer.base import Explainer
from app.explainer.rule_based import RuleBasedExplainer, format_duration
from app.hints import (
    FALLBACK_HINT,
    SILENCE_HINT,
    PatternKey,
    hint_for,
    merge_evidence,
    rank_patterns,
    top_patterns,
)
from app.models import (
    Escalation,
    GroupCard,
    GroupSummary,
    Incident,
    IncidentEvent,
    IncidentState,
    IncidentSummary,
    MemberIncident,
    NotifyRequest,
    PatternRef,
    Severity,
    SignalType,
    TimelineEntry,
)
from app.severity import STATISTICAL_SIGNALS, compute_severity
from app.store import Store

logger = logging.getLogger(__name__)

SIGNAL_LABELS: dict[SignalType, str] = {
    SignalType.ERROR_RATE: "error rate anomaly",
    SignalType.LATENCY: "latency anomaly",
    SignalType.SILENCE: "service went silent",
    SignalType.NEW_PATTERN: "new error pattern",
    SignalType.PATTERN_SURGE: "error pattern surge",
}

# ---- mobile-friendly SNS notifications (§6.13-adjacent; not in CLAUDE.md, added for the demo) --

SMS_WRAP_WIDTH = 45  # short lines for a phone screen

# The subject's plain-language kind ("database slowdown") is read off the FIRST matching entry
# in the origin incident's own timeline, not a live/growing set of signal types -- entries are
# only ever appended, never reordered or removed, so the word is fixed the moment the incident
# opens and cannot later flip (e.g. from "slowdown" to "errors") as new signal types appear.
_KIND_BY_EVENT_LABEL = {
    SIGNAL_LABELS[SignalType.SILENCE]: "outage",
    SIGNAL_LABELS[SignalType.LATENCY]: "slowdown",
    SIGNAL_LABELS[SignalType.ERROR_RATE]: "errors",
}

# A pattern template's dynamic-token placeholders, replaced so a quoted pattern reads as prose
# rather than showing raw tokens like "<NUM>" (CLAUDE.md §6.5's mask, in reverse for humans).
_PLACEHOLDER_WORDS = {
    "<UUID>": "an ID",
    "<EMAIL>": "an email address",
    "<IP>": "an IP address",
    "<HEX>": "a code",
    "<NUM>": "N",
}


def _readable_pattern(template: str) -> str:
    text = template
    for token, word in _PLACEHOLDER_WORDS.items():
        text = text.replace(token, word)
    return text


# The canonical hint text (CLAUDE.md §6.12; also shown verbatim in the dashboard's HINT panel)
# is a full sentence. The SNS email is read on a phone, so CHECK FIRST uses a shorter, imperative
# phrasing of the same advice instead -- a display-only concern, so the mapping lives here rather
# than changing the hint text everyone else (dashboard, explainer) relies on.
_CONCISE_HINTS = {
    "Database or downstream dependency: check DB health, latency and connection pool.": (
        "Check the database: health, latency, connection pool."
    ),
    "Authentication/credentials: check keys, certificates, recent config changes.": (
        "Check auth: keys, certificates, recent config changes."
    ),
    "Resource exhaustion: check memory/disk on the host.": "Check the host: memory, disk space.",
    "Downstream service unavailable: check the dependency's health.": (
        "Check the downstream service's health."
    ),
    SILENCE_HINT: "Check the service is running and shipping logs.",
    FALLBACK_HINT: "Compare with recent deploys and config changes.",
}


def _concise_hint(hint: str) -> str:
    return _CONCISE_HINTS.get(hint, hint)


def _wrap(text: str, width: int = SMS_WRAP_WIDTH) -> list[str]:
    return textwrap.wrap(text, width=width, break_long_words=False) or [text]


def priority_key(incident: IncidentSummary | GroupSummary) -> tuple[int, int, int, float]:
    """Sort key for the feed: open first, then severity, NEW-pattern incidents above
    surge-only ones, then the most recent."""
    return (
        0 if incident.state is IncidentState.OPEN else 1,
        -int(incident.current_severity),
        0 if incident.has_new_pattern else 1,
        -(incident.resolved_at or incident.opened_at),
    )


@dataclass
class _Track:
    """Per-service bookkeeping between ticks."""

    streak: int = 0  # consecutive ticks with a qualifying signal (while no incident is open)
    streak_started: float | None = None
    pending: dict[SignalType, tuple[float, str]] = field(default_factory=dict)  # first sighting
    calm_streak: int = 0  # consecutive calm ticks (while an incident is open)
    silence_since: float | None = None


@dataclass
class _Meta:
    seen_types: set[SignalType]
    signature: tuple[Any, ...] | None = None


class IncidentManager:
    """Turns per-tick evaluations into a small number of meaningful incidents.

    - **Persistence:** a service needs a signal on ``PERSIST_TICKS`` consecutive ticks.
    - **One incident per service**, updated in place; incidents on connected services join one
      correlation group (one outage = one group).
    - **Hysteresis:** an incident resolves only after ``RESOLVE_TICKS`` consecutive calm ticks
      (every z below ``RESOLVE_Z``, not just below the ``OPEN_Z`` that raised it).
    - **Cooldown:** the same service anomalous again within ``COOLDOWN_SECONDS`` of resolving
      reopens the same incident.
    - **Notifications** are decided per group, never per service or per tick: open at HIGH+,
      escalation into a higher HIGH/CRITICAL level, and the resolve of a group that notified.

    While a service has an open incident its baseline is frozen; ``open_services()`` is what the
    pipeline passes as ``frozen`` to the aggregator and detectors.
    """

    def __init__(
        self,
        clock: Clock,
        aggregator: Aggregator,
        baseline: Baseline,
        graph: DependencyGraph,
        *,
        settings: Settings | None = None,
        store: Store | None = None,
        explainer: Explainer | None = None,
    ) -> None:
        self._clock = clock
        self._agg = aggregator
        self._baseline = baseline
        self._settings = settings or get_settings()
        self._store = store
        self._explainer: Explainer = explainer or RuleBasedExplainer()
        self._engine = CorrelationEngine(graph, self._settings.CORRELATION_WINDOW_SECONDS)
        self._incidents: dict[str, Incident] = {}
        self._meta: dict[str, _Meta] = {}
        self._open: dict[str, str] = {}  # service -> id of its open incident
        self._last_resolved: dict[str, str] = {}  # service -> id of its latest resolved incident
        self._tracks: dict[str, _Track] = {}
        self._evidence: dict[str, dict[PatternKey, PatternRef]] = {}  # per group, at its worst
        self._seq = 0
        self.notifications_emitted = 0
        # per-tick scratch
        self._active_now: set[str] = set()
        self._touched: dict[str, None] = {}
        self._opened: dict[str, str] = {}
        self._resolved: set[str] = set()
        self._fresh_groups: set[str] = set()

    # ---- read API ---------------------------------------------------------------------

    def open_services(self) -> set[str]:
        """Services with an open incident: their baselines must not learn."""
        return set(self._open)

    def service_status(self, service: str) -> tuple[Severity | None, bool]:
        """(severity of the service's open incident or None, whether it is silent right now)."""
        track = self._tracks.get(service)
        silent = track is not None and track.silence_since is not None
        incident_id = self._open.get(service)
        severity = self._incidents[incident_id].current_severity if incident_id else None
        return severity, silent

    def get(self, incident_id: str) -> Incident | None:
        incident = self._incidents.get(incident_id)
        return incident.model_copy(deep=True) if incident else None

    def incidents(self, state: str = "all") -> list[Incident]:
        """Copies, sorted by ``priority_key``. ``state``: open | resolved | all."""
        wanted = {
            "open": {IncidentState.OPEN},
            "resolved": {IncidentState.RESOLVED},
        }.get(state.lower(), set(IncidentState))
        chosen = [i for i in self._incidents.values() if i.state in wanted]
        return [i.model_copy(deep=True) for i in sorted(chosen, key=priority_key)]

    def group(self, group_id: str) -> CorrelationGroup | None:
        return self._engine.groups.get(group_id)

    def group_incidents(self, group_id: str) -> list[Incident]:
        group = self._engine.groups.get(group_id)
        if group is None:
            return []
        return [self._incidents[m.incident_id].model_copy(deep=True) for m in group.members]

    @property
    def groups_opened(self) -> int:
        """Distinct outages so far (merged groups count once): the "1 incident" of the demo."""
        return len(self._engine.groups)

    @staticmethod
    def summary(incident: Incident) -> IncidentSummary:
        return IncidentSummary(**{f: getattr(incident, f) for f in IncidentSummary.model_fields})

    # ---- group cards: what the user sees -----------------------------------------------

    def resolve_group_id(self, any_id: str) -> str | None:
        """A group id, or the id of one of its member incidents, to a group id."""
        if any_id in self._engine.groups:
            return any_id
        incident = self._incidents.get(any_id)
        return incident.group_id if incident else None

    def group_card(self, any_id: str) -> GroupCard | None:
        """The full card for a group (with the explanation), by group or member incident id."""
        group_id = self.resolve_group_id(any_id)
        if group_id is None:
            return None
        card = self._card(self._engine.groups[group_id])
        card.explanation = self._explainer.explain(card)
        return card

    def group_summary(self, any_id: str) -> GroupSummary | None:
        group_id = self.resolve_group_id(any_id)
        if group_id is None:
            return None
        return self._summarize(self._card(self._engine.groups[group_id]))

    def group_summaries(self, state: str = "all") -> list[GroupSummary]:
        """One summary per outage, sorted by priority. ``state``: open | resolved | all."""
        wanted = {"open": {IncidentState.OPEN}, "resolved": {IncidentState.RESOLVED}}.get(
            state.lower(), set(IncidentState)
        )
        summaries = [self._summarize(self._card(g)) for g in self._engine.groups.values()]
        return sorted((s for s in summaries if s.state in wanted), key=priority_key)

    @staticmethod
    def _summarize(card: GroupCard) -> GroupSummary:
        return GroupSummary.model_validate(card.model_dump(include=set(GroupSummary.model_fields)))

    @staticmethod
    def _member(incident: Incident) -> MemberIncident:
        return MemberIncident.model_validate(
            incident.model_dump(include=set(MemberIncident.model_fields))
        )

    def _card(self, group: CorrelationGroup) -> GroupCard:
        """Assemble a group's card from its member incidents (no explanation yet)."""
        members = sorted(
            (self._incidents[m.incident_id] for m in group.members), key=lambda i: i.opened_at
        )
        open_members = [i for i in members if i.state is IncidentState.OPEN]
        origin = self._engine.origin(group)
        origin_incident = next(i for i in members if i.service == origin)
        if open_members:  # open members are refreshed every tick; resolved ones are frozen
            rep = (
                origin_incident if origin_incident.state is IncidentState.OPEN else open_members[0]
            )
        else:
            rep = max(members, key=lambda i: i.resolved_at or 0.0)
        pool = open_members or members
        what = origin_incident.what or rep.what
        others = len(set(group.services)) - 1
        started = min(i.anomaly_started_at or i.opened_at for i in members)
        end = group.resolved_at if group.resolved_at is not None else self._clock.now()
        return GroupCard(
            id=group.id,
            state=IncidentState.OPEN if group.is_open else IncidentState.RESOLVED,
            current_severity=max(i.current_severity for i in pool),
            max_severity=max(i.max_severity for i in members),
            opened_at=group.opened_at,
            resolved_at=group.resolved_at,
            title=(f"{origin}: {what}" if what else origin)
            + (f" (+{others} more service{'s' if others > 1 else ''})" if others > 0 else ""),
            origin=origin,
            affected_services=list(dict.fromkeys(group.services)),
            has_new_pattern=any(i.has_new_pattern for i in members),
            notified=group.notified or group.notified_ever,
            incidents=[self._member(i) for i in members],
            what=what,
            duration_s=max(0.0, end - started),
            failed_ops=sum(i.failed_ops for i in members),
            peak_z=max(i.peak_z for i in members),
            hint=rep.hint,
            patterns=[p.model_copy() for p in rep.patterns],
            samples=list(origin_incident.samples or rep.samples),
            story=sorted((e for i in members for e in i.timeline), key=lambda e: e.t),
            blast_radius=rep.blast_radius.model_copy(deep=True) if rep.blast_radius else None,
        )

    # ---- the tick ---------------------------------------------------------------------

    def process(self, evaluations: Mapping[str, Evaluation]) -> list[IncidentEvent]:
        """Advance every service one tick. Returns the state changes, in order."""
        now = self._clock.now()
        self._active_now = set()
        self._touched = {}
        self._opened = {}
        self._resolved = set()
        self._fresh_groups = set()

        for service, ev in evaluations.items():
            if not ev.ready:
                continue
            track = self._tracks.setdefault(service, _Track())
            self._track_silence(track, ev, now)
            incident_id = self._open.get(service)
            if incident_id is None:
                self._consider_open(service, ev, track, now)
            else:
                self._update_open(self._incidents[incident_id], ev, track, now)

        self._refresh_groups(now)
        notifies = self._settle_groups(now)
        return self._build_events(now, notifies)

    # ---- per-service state machine ----------------------------------------------------

    @staticmethod
    def _track_silence(track: _Track, ev: Evaluation, now: float) -> None:
        if any(sig.type is SignalType.SILENCE for sig in ev.signals):
            if track.silence_since is None:
                track.silence_since = now
        else:
            track.silence_since = None

    def _consider_open(self, service: str, ev: Evaluation, track: _Track, now: float) -> None:
        if not ev.signals:  # any tick without a signal breaks the streak: that is persistence
            track.streak = 0
            track.streak_started = None
            track.pending.clear()
            return
        track.streak += 1
        if track.streak == 1:
            track.streak_started = now
        for sig in ev.signals:
            track.pending.setdefault(sig.type, (now, sig.detail))
        self._active_now.add(service)
        if track.streak >= self._settings.PERSIST_TICKS:
            self._open_incident(service, ev, track, now)

    def _open_incident(self, service: str, ev: Evaluation, track: _Track, now: float) -> None:
        last_id = self._last_resolved.get(service)
        last = self._incidents.get(last_id) if last_id else None
        cooldown = self._settings.COOLDOWN_SECONDS
        if last is not None and last.resolved_at is not None and now - last.resolved_at <= cooldown:
            incident = self._reopen(last, track, now)
        else:
            incident = self._create(service, track, now)

        self._open[service] = incident.id
        self._touched[incident.id] = None
        incident.signals = list(ev.signals)
        incident.peak_z = max([incident.peak_z, *(s.z for s in ev.signals)])
        track.calm_streak = 0
        track.pending.clear()
        track.streak = 0

        severity = self._severity_now(incident, now)
        incident.current_severity = severity
        incident.max_severity = max(incident.max_severity, severity)
        incident.timeline.append(
            TimelineEntry(
                t=now, service=service, event=self._opened[incident.id], severity=severity
            )
        )

    def _create(self, service: str, track: _Track, now: float) -> Incident:
        self._seq += 1
        incident_id = f"inc-{int(now)}-{self._seq}"
        started = track.streak_started if track.streak_started is not None else now
        member = Member(incident_id, service, opened_at=now, deviated_at=started)
        group, moved = self._engine.join(member, now)
        if len(group.members) == 1:
            self._fresh_groups.add(group.id)
        for moved_id in moved:  # groups merged: the absorbed incidents change group
            absorbed = self._evidence.pop(self._incidents[moved_id].group_id, {})
            merge_evidence(self._evidence.setdefault(group.id, {}), absorbed.values())
            self._incidents[moved_id].group_id = group.id
            self._touched[moved_id] = None
        incident = Incident(
            id=incident_id,
            service=service,
            group_id=group.id,
            state=IncidentState.OPEN,
            current_severity=Severity.LOW,
            max_severity=Severity.LOW,
            opened_at=now,
            anomaly_started_at=started,
        )
        # The story starts when each signal type FIRST appeared, not when persistence let us in.
        for kind, (t, detail) in sorted(track.pending.items(), key=lambda kv: kv[1][0]):
            incident.timeline.append(
                TimelineEntry(t=t, service=service, event=SIGNAL_LABELS[kind], detail=detail)
            )
        self._incidents[incident_id] = incident
        self._meta[incident_id] = _Meta(seen_types=set(track.pending))
        self._opened[incident_id] = "opened"
        return incident

    def _reopen(self, incident: Incident, track: _Track, now: float) -> Incident:
        incident.state = IncidentState.OPEN
        incident.resolved_at = None
        incident.anomaly_started_at = track.streak_started or now
        group = self._engine.groups.get(incident.group_id)
        if group is not None and not group.is_open:
            group.resolved_at = None
            self._fresh_groups.add(group.id)
        self._meta[incident.id].seen_types |= set(track.pending)
        self._opened[incident.id] = "reopened"
        return incident

    def _update_open(self, incident: Incident, ev: Evaluation, track: _Track, now: float) -> None:
        meta = self._meta[incident.id]
        if ev.signals:
            self._active_now.add(incident.service)
            incident.signals = list(ev.signals)
            incident.peak_z = max([incident.peak_z, *(s.z for s in ev.signals)])
            for sig in ev.signals:
                if sig.type not in meta.seen_types:
                    meta.seen_types.add(sig.type)
                    incident.timeline.append(
                        TimelineEntry(
                            t=now,
                            service=incident.service,
                            event=SIGNAL_LABELS[sig.type],
                            severity=incident.current_severity,
                            detail=sig.detail,
                        )
                    )

        s = self._settings
        calm = ev.is_calm(s.RESOLVE_Z, s.ABS_FLOOR_ERROR, s.SILENCE_RESOLVE_RATIO)
        track.calm_streak = track.calm_streak + 1 if calm else 0
        if track.calm_streak >= s.RESOLVE_TICKS:
            self._resolve(incident, track, now)

    def _resolve(self, incident: Incident, track: _Track, now: float) -> None:
        incident.state = IncidentState.RESOLVED
        incident.resolved_at = now
        incident.duration_s = now - (incident.anomaly_started_at or incident.opened_at)
        incident.timeline.append(
            TimelineEntry(
                t=now,
                service=incident.service,
                event="resolved",
                severity=incident.current_severity,
                detail=f"calm for {self._settings.RESOLVE_TICKS} consecutive ticks",
            )
        )
        del self._open[incident.service]
        self._last_resolved[incident.service] = incident.id
        track.streak = track.calm_streak = 0
        track.streak_started = None
        track.pending.clear()
        track.silence_since = None
        self._resolved.add(incident.id)
        self._touched[incident.id] = None

    # ---- severity ---------------------------------------------------------------------

    def _severity_now(self, incident: Incident, now: float) -> Severity:
        group = self._engine.groups[incident.group_id]
        anomalous = sum(
            1 for m in group.members if self._incidents[m.incident_id].state is IncidentState.OPEN
        )
        silence_since = self._tracks[incident.service].silence_since
        severity = compute_severity(
            incident.signals,
            error_rate=self._agg.window(incident.service).error_rate,
            anomaly_duration_s=now - (incident.anomaly_started_at or incident.opened_at),
            silence_duration_s=0.0 if silence_since is None else now - silence_since,
            anomalous_services=max(anomalous, 1),
            settings=self._settings,
        )
        return severity if severity is not None else incident.current_severity

    def _apply_severity(self, incident: Incident, new: Severity, now: float) -> None:
        old = incident.current_severity
        if new == old:
            return
        incident.timeline.append(
            TimelineEntry(
                t=now,
                service=incident.service,
                event="escalated" if new > old else "de-escalated",
                severity=new,
                detail=f"{old.name} -> {new.name}",
            )
        )
        if new > old:
            incident.escalations.append(Escalation(t=now, from_severity=old, to_severity=new))
        incident.current_severity = new
        incident.max_severity = max(incident.max_severity, new)

    # ---- group-level view: severity, origin, patterns, story --------------------------

    def _refresh_groups(self, now: float) -> None:
        s = self._settings
        for group in self._engine.open_groups():
            open_incidents = [
                self._incidents[m.incident_id]
                for m in group.members
                if self._incidents[m.incident_id].state is IncidentState.OPEN
            ]
            if not open_incidents:
                continue
            # 1) severity first (it adds timeline entries the story must include)
            for inc in open_incidents:
                if inc.service in self._active_now:
                    self._apply_severity(inc, self._severity_now(inc, now), now)

            # 2) everything that is about the whole outage
            origin = self._engine.origin(group)
            impacted = [inc.service for inc in open_incidents]
            # The window forgets an outage as it drains; the card keeps it as it was at its worst.
            evidence = self._evidence.setdefault(group.id, {})
            merge_evidence(
                evidence, rank_patterns(impacted, self._agg, self._baseline, s, limit=None)
            )
            patterns = top_patterns(evidence)
            silent = any(SignalType.SILENCE in self._meta[i.id].seen_types for i in open_incidents)
            hint = hint_for(
                patterns,
                [g for i in open_incidents for g in i.signals],
                self._agg,
                s,
                silent=silent,
            )
            blast = self._engine.blast_radius(origin, impacted)
            story = sorted(
                (e for m in group.members for e in self._incidents[m.incident_id].timeline),
                key=lambda e: e.t,
            )
            affected = list(dict.fromkeys(group.services))

            for inc in open_incidents:
                window = self._agg.window(inc.service)
                inc.origin = origin
                inc.affected_services = affected
                inc.blast_radius = blast
                inc.patterns = patterns
                inc.hint = hint
                inc.story = story
                inc.samples = self._samples(inc, patterns) or inc.samples
                inc.failed_ops = window.errors
                inc.duration_s = now - (inc.anomaly_started_at or inc.opened_at)
                inc.what = self._what(inc) or inc.what
                inc.title = f"{inc.service}: {inc.what}" if inc.what else inc.service
                inc.has_new_pattern = SignalType.NEW_PATTERN in self._meta[inc.id].seen_types

    def _samples(self, incident: Incident, patterns: Iterable[PatternRef]) -> list[str]:
        own = {p.pattern_id for p in patterns if p.service == incident.service}
        return self._agg.samples(incident.service, 3, own or None) or self._agg.samples(
            incident.service, 3
        )

    @staticmethod
    def _what(incident: Incident) -> str:
        stat = [sig.detail for sig in incident.signals if sig.type in STATISTICAL_SIGNALS]
        if stat:
            return "; ".join(stat)
        return incident.signals[0].detail if incident.signals else ""

    # ---- resolution of groups and notifications ---------------------------------------

    def _settle_groups(self, now: float) -> list[NotifyRequest]:
        notifies: list[NotifyRequest] = []
        for group in list(self._engine.groups.values()):
            if not group.is_open:
                continue
            incidents = [self._incidents[m.incident_id] for m in group.members]
            if all(i.state is IncidentState.RESOLVED for i in incidents):
                group.resolved_at = now
                if group.notified:
                    peak = max(i.max_severity for i in incidents)
                    notifies.append(self._notify("resolved", group, peak, now))
                    group.notified, group.notified_severity = False, None
                continue

            level = max(i.current_severity for i in incidents if i.state is IncidentState.OPEN)
            if level >= Severity.HIGH and (
                group.notified_severity is None or level > group.notified_severity
            ):
                kind = "opened" if group.id in self._fresh_groups else "escalated"
                group.notified = group.notified_ever = True
                group.notified_severity = level
                notifies.append(self._notify(kind, group, level, now))
            for inc in incidents:
                if inc.state is IncidentState.OPEN and inc.notified != group.notified:
                    inc.notified = group.notified
                    self._touched[inc.id] = None
        self.notifications_emitted += len(notifies)
        return notifies

    def _notify(
        self, kind: str, group: CorrelationGroup, severity: Severity, now: float
    ) -> NotifyRequest:
        """Build the SNS notification: short enough to read on a phone at a glance. The subject
        alone carries severity and title (the body does not repeat them); the first body line
        says whether it is still active, everything else is there for someone who scrolls
        further. IDs are pushed to the very bottom."""
        origin = self._engine.origin(group)
        member = next(m for m in group.members if m.service == origin)
        origin_incident = self._incidents[member.incident_id]
        card = self._card(group)  # the full, merged evidence (what/timeline/patterns/hint)
        tz = ZoneInfo(self._settings.DISPLAY_TIMEZONE)
        n = len(card.affected_services) or 1
        plural = "s" if n != 1 else ""
        title = f"{origin} {self._plain_kind(origin_incident)}"

        members = [self._incidents[m.incident_id] for m in group.members]
        started = min(i.anomaly_started_at or i.opened_at for i in members)

        if kind == "resolved":
            subject = f"[RESOLVED] {title} - {n} service{plural} affected"
            body = self._resolved_body(card, title)
        else:
            subject = f"[{severity.name}] {title} - {n} service{plural} affected"
            body = self._active_body(card, origin, tz, started, origin_incident)

        payload: dict[str, Any] = {
            "kind": kind,
            "incident_id": member.incident_id,
            "group_id": group.id,
            "severity": severity.name,
            "origin": origin,
            "affected_services": card.affected_services,
            "what": card.what,
            "hint": card.hint,
            "patterns": [p.model_dump() for p in card.patterns],
            "opened_at": group.opened_at,
            "at": now,
        }
        return NotifyRequest(
            kind=kind,  # type: ignore[arg-type]
            incident_id=member.incident_id,
            group_id=group.id,
            severity=severity,
            subject=subject[:100],
            body=body,
            payload=payload,
        )

    @staticmethod
    def _plain_kind(origin_incident: Incident) -> str:
        """A one-or-two-word plain-language description of what kind of trouble this is, for
        the subject line ("database slowdown", "auth-service errors"): the first symptom in the
        incident's own timeline that actually says what broke (skipping new-pattern/surge
        entries, which say a pattern is unusual but not what kind of anomaly it is). Timeline
        entries are only ever appended, so this word is fixed forever once the incident opens."""
        for entry in origin_incident.timeline:
            found = _KIND_BY_EVENT_LABEL.get(entry.event)
            if found is not None:
                return found
        return "anomaly"

    @staticmethod
    def _multiplier(value: float, baseline: float) -> str:
        """"Nx" against a baseline, truncated (3.9x reads as "3x": "at least this much worse",
        not a rounded estimate). Empty when there is no usable baseline to divide by."""
        return f"{int(value / baseline)}x" if baseline > 0 else ""

    @classmethod
    def _whats_wrong_lines(cls, origin_incident: Incident) -> list[str]:
        signals = origin_incident.signals
        error = next((s for s in signals if s.type is SignalType.ERROR_RATE), None)
        latency = next((s for s in signals if s.type is SignalType.LATENCY), None)
        lines: list[str] = []
        if error is not None:
            mult = cls._multiplier(error.value, error.baseline_median)
            pct = f"{error.value * 100:.0f}% vs {error.baseline_median * 100:.0f}%"
            lines.append(f"Error rate {mult} normal ({pct})" if mult else f"Error rate {pct}")
        if latency is not None and latency.baseline_median > 0:
            mult = cls._multiplier(latency.value, latency.baseline_median)
            lines.extend(
                _wrap(
                    f"{mult} slower than normal "
                    f"({latency.value:.0f} ms vs {latency.baseline_median:.0f} ms)"
                )
            )
        if not lines:
            lines.extend(_wrap(origin_incident.what or "Anomaly detected"))
        lines.append(f"{origin_incident.failed_ops} failed operations")
        return lines

    @staticmethod
    def _key_moments(timeline: list[TimelineEntry]) -> list[tuple[float, str]]:
        """At most 4 moments from the ORIGIN's own timeline (not the group's merged story, which
        would mix in other services' opens and read as duplicated, out-of-context lines): how it
        started, when it opened, and its escalations -- the shape of the incident, not the log."""
        moments: list[tuple[float, str]] = []
        if timeline and timeline[0].event not in ("opened", "reopened"):
            moments.append((timeline[0].t, "first signs of trouble"))
        for e in timeline:
            if e.event in ("opened", "reopened"):
                moments.append((e.t, "reopened" if e.event == "reopened" else "incident opened"))
            elif e.event == "escalated":
                moments.append((e.t, f"escalated to {e.severity.name if e.severity else '?'}"))
        deduped = list(dict.fromkeys(moments))
        if len(deduped) > 4:
            deduped = deduped[:1] + deduped[-3:]  # keep the start, then the latest progression
        return deduped

    def _local_clock(self, t: float, tz: ZoneInfo) -> str:
        """Includes seconds: escalations often land within the same minute, and without seconds
        the WHAT HAPPENED progression looks like everything happened at once."""
        dt = datetime.fromtimestamp(t, tz)
        hour = dt.strftime("%I").lstrip("0") or "12"
        return f"{hour}:{dt.strftime('%M:%S %p')}"

    def _active_body(
        self,
        card: GroupCard,
        origin: str,
        tz: ZoneInfo,
        started: float,
        origin_incident: Incident,
    ) -> str:
        started_str = self._local_clock(started, tz)
        tzname = datetime.fromtimestamp(started, tz).tzname()
        # Severity and title are the subject line; the body does not repeat them.
        lines = [f"Started {started_str} {tzname} - still active.", "", "CHECK FIRST:"]
        lines.extend(_wrap(_concise_hint(card.hint or FALLBACK_HINT)))
        top = card.patterns[0] if card.patterns else None
        if top is not None:
            phrase = _readable_pattern(top.template)
            lines.extend(_wrap(f'Seen: "{phrase}" (x{top.count})'))
        lines += ["", "WHAT'S WRONG:", *self._whats_wrong_lines(origin_incident)]
        lines += ["", "AFFECTED:"]
        for svc in card.affected_services:
            lines.append(f"- {svc}" + (" (origin)" if svc == origin else ""))
        lines += ["", "WHAT HAPPENED:"]
        for t, text in self._key_moments(origin_incident.timeline):
            lines.append(f"{self._local_clock(t, tz)} - {text}")
        lines += [
            "",
            f"Dashboard: {self._settings.DASHBOARD_URL.rstrip('/')}/?incident={card.id}",
            f"Ref: {card.id}",
        ]
        return "\n".join(lines)

    def _resolved_body(self, card: GroupCard, title: str) -> str:
        affected = ", ".join(card.affected_services)
        summary = (
            f"RESOLVED: {title}. Lasted {format_duration(card.duration_s)}. "
            f"Peak: {card.max_severity.name}. Affected: {affected}. No action needed."
        )
        lines = [
            *_wrap(summary),
            "",
            f"Dashboard: {self._settings.DASHBOARD_URL.rstrip('/')}/?incident={card.id}",
            f"Ref: {card.id}",
        ]
        return "\n".join(lines)

    # ---- events and persistence -------------------------------------------------------

    def _signature(self, inc: Incident) -> tuple[Any, ...]:
        return (
            inc.state,
            inc.current_severity,
            inc.origin,
            tuple(inc.affected_services),
            tuple(sorted((p.service, p.pattern_id) for p in inc.patterns)),
            inc.hint,
            inc.notified,
            len(inc.timeline),
        )

    def _build_events(self, now: float, notifies: list[NotifyRequest]) -> list[IncidentEvent]:
        by_incident = {n.incident_id: n for n in notifies}
        for incident_id in by_incident:
            self._touched[incident_id] = None
        for incident in self._incidents.values():  # open incidents whose card moved
            if incident.state is IncidentState.OPEN:
                meta = self._meta[incident.id]
                if meta.signature != self._signature(incident):
                    self._touched[incident.id] = None

        events: list[IncidentEvent] = []
        for incident_id in self._touched:
            incident = self._incidents[incident_id]
            meta = self._meta[incident_id]
            previous = meta.signature
            meta.signature = self._signature(incident)
            if incident_id in self._resolved:
                kind, transition = "resolved", "resolved"
            elif incident_id in self._opened:
                kind, transition = "opened", self._opened[incident_id]
            else:
                kind = "updated"
                transition = (
                    "severity_changed"
                    if previous is not None and previous[1] != incident.current_severity
                    else "updated"
                )
            event = IncidentEvent(
                kind=kind,  # type: ignore[arg-type]
                transition=transition,
                incident=incident.model_copy(deep=True),
                notify=by_incident.get(incident_id),
            )
            events.append(event)
            self._persist(event, now)
        return events

    def _persist(self, event: IncidentEvent, now: float) -> None:
        if self._store is None:
            return
        try:
            incident = event.incident
            self._store.upsert_incident(incident)
            self._store.add_event(
                incident.id, now, event.transition, incident.current_severity, incident
            )
        except Exception as exc:  # the audit trail must never stop monitoring
            logger.warning("store write failed: %s", type(exc).__name__)
