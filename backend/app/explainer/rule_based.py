"""Deterministic rule-based explanation (always on; also the fallback for the LLM one)."""

from __future__ import annotations

from app.hints import FALLBACK_HINT, SILENCE_HINT
from app.models import Explanation, GroupCard, IncidentState, PatternRef, SignalType

LABEL = "Rule-based summary (deterministic)"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _pattern_line(p: PatternRef) -> str:
    tag = "NEW pattern" if p.is_new else "Surging pattern"
    return (
        f"{tag} '{p.template}' on {p.service}: seen {p.count}x in the last minute "
        f"(normally {p.expected_per_min:.0f}/min)"
    )


class RuleBasedExplainer:
    """Builds the explanation from the card alone: same card in, same words out.

    It only restates evidence the detectors produced (metrics vs baseline, patterns, the
    keyword hint) and always calls the cause *probable*, never confirmed.
    """

    def explain(self, card: GroupCard) -> Explanation:
        origin = card.origin or (card.affected_services[0] if card.affected_services else "unknown")
        others = [s for s in card.affected_services if s != origin]
        resolved = card.state is IncidentState.RESOLVED
        severity = card.max_severity.name if resolved else card.current_severity.name
        silent = any(
            sig.type is SignalType.SILENCE for m in card.incidents for sig in m.signals
        ) or any(e.event == "service went silent" for e in card.story)

        parts = [
            f"{origin} is the probable origin of a {severity} incident"
            + (f" that also affects {_join(others)}." if others else ".")
        ]
        if card.what:
            parts.append(f"What changed: {card.what}.")
        span = format_duration(card.duration_s)
        if resolved:
            parts.append(f"It lasted {span} and has resolved; the worst severity was {severity}.")
        else:
            parts.append(
                f"It has lasted {span}, with {card.failed_ops} failed operations in the current "
                "window."
            )
        if card.patterns:
            parts.append(_pattern_line(card.patterns[0]) + ".")

        cause = f"Probable cause (not confirmed): {card.hint or FALLBACK_HINT}"
        if len(card.patterns) > 1:
            cause += f" Also seen: {_join([repr(p.template) for p in card.patterns[1:]])}."

        return Explanation(
            is_incident=True,
            summary=" ".join(parts),
            probable_cause=cause,
            affected_services=list(card.affected_services),
            recommended_checks=self._checks(card, origin, others, silent),
            confidence=self._confidence(card, others, silent),
            source="rule_based",
            label=LABEL,
        )

    @staticmethod
    def _checks(card: GroupCard, origin: str, others: list[str], silent: bool) -> list[str]:
        checks = []
        if card.hint and card.hint != FALLBACK_HINT:
            checks.append(card.hint)
        checks.append(f"Look at {origin} first: its logs and dashboards around the time it opened.")
        if others:
            checks.append(
                f"Then check the services that depend on it or were affected with it: "
                f"{_join(others)}."
            )
        if silent:
            checks.append("Confirm the service's host or container is running and shipping logs.")
        checks.append("Compare with recent deployments and configuration changes.")
        return checks

    @staticmethod
    def _confidence(card: GroupCard, others: list[str], silent: bool) -> str:
        """Higher when independent evidence agrees: a recognised failure signature, a NEW
        pattern, and more than one service showing it. Never above "medium" from one alone."""
        signature = card.hint not in ("", FALLBACK_HINT) and card.hint != SILENCE_HINT
        evidence = sum([signature, card.has_new_pattern, bool(others)])
        if silent:
            return "high" if card.max_severity.name == "CRITICAL" else "medium"
        if evidence >= 3:
            return "high"
        return "medium" if evidence == 2 else "low"
