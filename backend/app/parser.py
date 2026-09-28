"""Parse raw lines (regex, JSON fallback) into redacted, templated LogEvents."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.clock import Clock
from app.config import Settings, get_settings
from app.models import Level, LogEvent
from app.patterns import PatternMiner, RegexPatternMiner
from app.redaction import Redactor, get_redactor

UNKNOWN_SERVICE = "unknown"
MAX_STORED_CHARS = 2000  # applied after redaction, so a cut can never expose half a PHI value

_LEVELS: dict[str, Level] = {
    "DEBUG": Level.DEBUG,
    "INFO": Level.INFO,
    "WARN": Level.WARN,
    "WARNING": Level.WARN,
    "ERROR": Level.ERROR,
    "CRITICAL": Level.CRITICAL,
    "FATAL": Level.CRITICAL,
}


@dataclass(slots=True)
class _Fields:
    ts: Any  # str | float | None, resolved later
    level: Level
    service: str
    latency_ms: int | None
    message: str


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class LogParser:
    """Turns one raw line into a LogEvent. Never raises on bad input.

    Parsing happens first (in memory only); everything that leaves this class, both
    ``message`` and ``raw``, is already redacted. Error/warn lines also get a pattern template.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        settings: Settings | None = None,
        redactor: Redactor | None = None,
        miner: PatternMiner | None = None,
    ) -> None:
        settings = settings or get_settings()
        self._clock = clock
        self._regex = re.compile(settings.LOG_REGEX)
        self._redactor = redactor or get_redactor()
        self._miner = miner or RegexPatternMiner()
        self.unparsed = 0

    def parse(self, line: str) -> LogEvent:
        raw = line.rstrip("\r\n")
        # The redaction count is tracked once, on the raw line, which holds every PHI value
        # the message does.
        red_raw, _ = self._redactor.redact(raw)
        fields = self._parse_json(raw) if raw.lstrip().startswith("{") else None
        if fields is not None:
            # JSON is parsed from the original (redacting first could break the syntax), so
            # its message needs its own redaction pass.
            message, _ = self._redactor.redact(fields.message, track=False)
        else:
            # Everything else is parsed from the already-redacted line: one pass per line.
            # PHI never sits in the timestamp/level/service header, so the parse is unaffected.
            fields = self._parse_regex(red_raw)
            message = fields.message if fields else red_raw
        red_raw = red_raw[:MAX_STORED_CHARS]

        if fields is None:
            self.unparsed += 1
            return LogEvent(
                ts=self._clock.now(),
                level=Level.INFO,
                is_error=False,
                is_warn=False,
                service=UNKNOWN_SERVICE,
                message=red_raw,
                raw=red_raw,
                parsed=False,
            )

        message = message[:MAX_STORED_CHARS]
        is_error = fields.level in (Level.ERROR, Level.CRITICAL)
        is_warn = fields.level is Level.WARN
        pattern_id = template = None
        if is_error or is_warn:
            pattern = self._miner.mine(message)
            pattern_id, template = pattern.id, pattern.template
        return LogEvent(
            ts=self._resolve_ts(fields.ts),
            level=fields.level,
            is_error=is_error,
            is_warn=is_warn,
            service=fields.service,
            latency_ms=fields.latency_ms,
            message=message,
            raw=red_raw,
            pattern_id=pattern_id,
            template=template,
        )

    def _parse_regex(self, raw: str) -> _Fields | None:
        m = self._regex.match(raw)
        if m is None:
            return None
        groups = m.groupdict()
        level = _LEVELS.get((groups.get("level") or "").upper())
        service = groups.get("service")
        if level is None or not service:
            return None
        return _Fields(
            ts=groups.get("ts"),
            level=level,
            service=service,
            latency_ms=_to_int(groups.get("latency_ms")),
            message=(groups.get("message") or "").strip(),
        )

    @staticmethod
    def _parse_json(raw: str) -> _Fields | None:
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        level = _LEVELS.get(str(obj.get("level", "")).upper())
        service = obj.get("service")
        message = obj.get("msg", obj.get("message"))
        if level is None or not isinstance(service, str) or not service or message is None:
            return None
        return _Fields(
            ts=obj.get("ts", obj.get("timestamp")),
            level=level,
            service=service,
            latency_ms=_to_int(obj.get("latency_ms")),
            message=str(message).strip(),
        )

    def _resolve_ts(self, value: Any) -> float:
        """Epoch seconds from an ISO string or number; clock time if missing or unparsable."""
        try:
            if isinstance(value, bool) or value is None:
                raise ValueError
            if isinstance(value, (int, float)):
                return float(value) / 1000.0 if value > 1e11 else float(value)
            dt = datetime.fromisoformat(str(value).strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
        except (ValueError, OverflowError, OSError):
            return self._clock.now()
