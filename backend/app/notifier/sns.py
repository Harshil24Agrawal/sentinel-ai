"""SNS sink: HIGH/CRITICAL opens and escalations, and the resolve of a group that notified."""

from __future__ import annotations

from typing import Any

SUBJECT_LIMIT = 100  # SNS rejects longer subjects


class SnsSink:
    def __init__(self, client: Any, topic_arn: str) -> None:
        self._client = client
        self._topic_arn = topic_arn

    def publish(self, subject: str, body: str) -> None:
        # SNS subjects must be single-line ASCII
        clean = " ".join(subject.split()).encode("ascii", "replace").decode("ascii")
        self._client.publish(TopicArn=self._topic_arn, Subject=clean[:SUBJECT_LIMIT], Message=body)
