"""The live log feed: a ring buffer of redacted events and the per-tick WebSocket batch.

Everything here works on ``LogEvent``s the parser has already redacted, so nothing in the buffer
or in a batch can carry PHI. ERROR/CRITICAL and WARN lines always go out; INFO/DEBUG lines are
sampled so a busy tick cannot flood the browser.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Collection
from typing import Any

from app.config import Settings
from app.models import Level, LogEvent, LogLine

_ERRORS = (Level.ERROR, Level.CRITICAL)


def _line_of(event: LogEvent) -> LogLine:
    return LogLine(
        ts=event.ts,
        level=event.level,
        service=event.service,
        message=event.message,
        pattern_id=event.pattern_id,
    )


def _evenly(items: list[int], count: int) -> list[int]:
    """``count`` items spread evenly through ``items`` (all of them if there are fewer)."""
    if len(items) <= count:
        return items
    return [items[i * len(items) // count] for i in range(count)]


class LogFeed:
    def __init__(self, settings: Settings) -> None:
        self._info_per_tick = max(0, settings.LOG_INFO_PER_TICK)
        self._max_lines = max(0, settings.LOG_LINES_PER_MESSAGE)
        self._buffer: deque[LogLine] = deque(maxlen=max(1, settings.LOG_BUFFER_SIZE))
        self._tick: list[LogLine] = []

    def add(self, event: LogEvent) -> None:
        line = _line_of(event)
        self._buffer.append(line)
        self._tick.append(line)

    def recent(
        self, service: str | None = None, levels: Collection[Level] | None = None, limit: int = 200
    ) -> list[LogLine]:
        """The buffer, oldest first (newest last). Filters apply before ``limit``."""
        lines = [
            line
            for line in self._buffer
            if (service is None or line.service == service)
            and (levels is None or line.level in levels)
        ]
        return lines[-limit:] if limit > 0 else []

    def flush(self) -> dict[str, Any]:
        """The ``logs`` message payload for the tick just ended (and start the next one)."""
        tick, self._tick = self._tick, []
        errors = [i for i, line in enumerate(tick) if line.level in _ERRORS]
        warns = [i for i, line in enumerate(tick) if line.level is Level.WARN]
        infos = [i for i, line in enumerate(tick) if line.level not in (*_ERRORS, Level.WARN)]

        # Highest priority first, newest first within a level: what the cap keeps if it bites.
        keep: list[int] = []
        for group in (errors[::-1], warns[::-1], _evenly(infos, self._info_per_tick)[::-1]):
            keep.extend(group[: self._max_lines - len(keep)])
        kept = set(keep)
        return {
            "lines": [tick[i].model_dump(mode="json") for i in sorted(kept)],
            "sampled_out": sum(1 for i in infos if i not in kept),
            "capped": sum(1 for i in (*errors, *warns) if i not in kept),
        }
