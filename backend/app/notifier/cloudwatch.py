"""CloudWatch Logs sink: every incident state change, as JSON events."""

from __future__ import annotations

import json
from typing import Any


class CloudWatchSink:
    def __init__(self, client: Any, group: str, stream: str) -> None:
        self._client = client
        self._group = group
        self._stream = stream
        self._ready = False

    def ensure(self) -> None:
        """Create the log group and stream if missing (an existing one is fine). Public so the
        notifier's startup self-check can run it once, eagerly, and report the result."""
        if self._ready:
            return
        for create, kwargs in (
            (self._client.create_log_group, {"logGroupName": self._group}),
            (
                self._client.create_log_stream,
                {"logGroupName": self._group, "logStreamName": self._stream},
            ),
        ):
            try:
                create(**kwargs)
            except Exception as exc:
                if "AlreadyExists" not in type(exc).__name__ and "AlreadyExists" not in str(exc):
                    raise
        self._ready = True

    def put(self, records: list[dict[str, Any]]) -> None:
        """Send one batch; ``records`` are already redacted."""
        self.ensure()
        events = [
            {
                "timestamp": int(float(r.get("timestamp", 0)) * 1000),
                "message": json.dumps(r, default=str),
            }
            for r in records
        ]
        events.sort(key=lambda e: e["timestamp"])  # CloudWatch requires chronological order
        self._client.put_log_events(
            logGroupName=self._group, logStreamName=self._stream, logEvents=events
        )
