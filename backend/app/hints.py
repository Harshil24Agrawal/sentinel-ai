"""Probable cause: rank the group's patterns and map them to a hint. Never "root cause"."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from app.aggregator import Aggregator
from app.baseline import Baseline
from app.config import Settings, get_settings
from app.models import PatternRef, Signal, SignalType

DEFAULT_RULES: tuple[tuple[str, str], ...] = (
    (
        r"timeout|pool exhausted|deadlock|connection refused",
        "Database or downstream dependency: check DB health, latency and connection pool.",
    ),
    (
        r"\b(?:401|403)\b|unauthorized|token|signing key|certificate",
        "Authentication/credentials: check keys, certificates, recent config changes.",
    ),
    (
        r"OutOfMemory|heap|disk full|no space",
        "Resource exhaustion: check memory/disk on the host.",
    ),
    (
        r"\b5\d\d\b|unavailable|circuit",
        "Downstream service unavailable: check the dependency's health.",
    ),
)
SILENCE_HINT = (
    "Service stopped logging: check whether the process is alive and the log pipeline is intact."
)
FALLBACK_HINT = (
    "No known failure signature matched: compare with recent deploys and config changes, "
    "and check the affected services' dependencies."
)
# A pattern is worth showing if it is new, or running at least this many times its normal rate.
MIN_SURGE_RATIO_FOR_HINT = 2.0


def _rank_key(p: PatternRef) -> tuple[bool, float, int, str]:
    return (not p.is_new, -(p.surge_ratio or 0.0), -p.count, p.service)


def rank_patterns(
    services: Iterable[str],
    aggregator: Aggregator,
    baseline: Baseline,
    settings: Settings | None = None,
    limit: int | None = 3,
) -> list[PatternRef]:
    """The group's most telling error/warn patterns: NEW ones first, then by surge ratio.
    ``limit=None`` returns every candidate."""
    s = settings or get_settings()
    refs: list[PatternRef] = []
    for service in services:
        window = aggregator.window(service)
        ready_at = baseline.ready_at(service)
        for pid, count in window.pattern_counts.items():
            first_seen = aggregator.first_seen(service, pid)
            if first_seen is None:
                continue
            expected = aggregator.expected_count(service, pid)
            ratio = (count + 1) / (expected + 1)
            is_new = (
                ready_at is not None and first_seen > ready_at and count >= s.NEW_PATTERN_MIN_COUNT
            )
            if not is_new and ratio < MIN_SURGE_RATIO_FOR_HINT:
                continue  # an ordinary background pattern says nothing about this incident
            refs.append(
                PatternRef(
                    pattern_id=pid,
                    template=aggregator.template(service, pid) or pid,
                    count=count,
                    expected_per_min=expected * 60.0 / s.WINDOW_SECONDS,
                    is_new=is_new,
                    surge_ratio=ratio,
                    service=service,
                )
            )
    refs.sort(key=_rank_key)
    return refs if limit is None else refs[:limit]


PatternKey = tuple[str, str]  # (service, pattern_id)


def merge_evidence(peak: dict[PatternKey, PatternRef], current: Iterable[PatternRef]) -> None:
    """Fold this tick's patterns into an outage's evidence, keeping each pattern as it looked
    at its worst (highest count, and NEW once it has ever been NEW). The window forgets an
    outage as it drains; the incident card should not."""
    for ref in current:
        key = (ref.service, ref.pattern_id)
        old = peak.get(key)
        if old is None or ref.count > old.count:
            was_new = old.is_new if old else False
            peak[key] = ref.model_copy(update={"is_new": ref.is_new or was_new})
        elif ref.is_new and not old.is_new:
            peak[key] = old.model_copy(update={"is_new": True})


def top_patterns(peak: dict[PatternKey, PatternRef], limit: int = 3) -> list[PatternRef]:
    return sorted(peak.values(), key=_rank_key)[:limit]


def hint_for(
    patterns: Sequence[PatternRef],
    signals: Iterable[Signal],
    aggregator: Aggregator,
    settings: Settings | None = None,
    *,
    silent: bool = False,
) -> str:
    """The first keyword rule that matches the top-ranked pattern wins. Rules see both the
    template and a real sample line (templates mask numbers, so ``HTTP 503`` only survives in
    the sample). Extra rules from config are checked first. ``silent`` says a silence signal was
    seen at any point (the current ``signals`` can lose it once the service resumes)."""
    s = settings or get_settings()
    rules = [*s.HINT_RULES_EXTRA, *DEFAULT_RULES]
    for pattern in patterns:
        samples = aggregator.samples(pattern.service, 1, {pattern.pattern_id})
        text = f"{pattern.template}\n{samples[-1] if samples else ''}"
        for regex, hint in rules:
            if re.search(regex, text, re.IGNORECASE):
                return hint
    if silent or any(sig.type is SignalType.SILENCE for sig in signals):
        return SILENCE_HINT
    return FALLBACK_HINT
