"""Injectable clock (SystemClock, FakeClock)."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """Source of time as epoch seconds. Every module takes one."""

    def now(self) -> float: ...


class SystemClock:
    """Wall-clock time."""

    def now(self) -> float:
        return time.time()


class FakeClock:
    """Manually advanced clock for deterministic tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("FakeClock cannot go backwards")
        self._now += seconds
