"""Notifier: a queue and a worker, so AWS latency or failure can never block the pipeline."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.notifier.cloudwatch import CloudWatchSink
from app.notifier.dryrun import DryRunWriter
from app.notifier.sns import SnsSink
from app.redaction import Redactor

logger = logging.getLogger(__name__)

QUEUE_SIZE = 2000
CW_BATCH = 25


@dataclass(slots=True)
class Notification:
    """One outgoing payload. ``sns`` marks the ones that also go to SNS (HIGH+ only)."""

    record: dict[str, Any]
    sns: bool = False
    subject: str = ""
    body: str = ""
    attempts: int = field(default=0)


def aws_credentials_available(region: str) -> bool:
    try:
        import boto3

        return boto3.Session(region_name=region).get_credentials() is not None
    except Exception:
        return False


class Notifier:
    """Always writes the dry-run JSONL. Sends to CloudWatch (all state changes) and SNS (the
    important ones) only when ``AWS_ENABLED`` is true AND credentials exist.

    ``submit`` never blocks and never raises: a full queue drops the item and counts it; the
    worker retries AWS calls with backoff off the event loop and gives up quietly.
    """

    def __init__(
        self,
        settings: Settings,
        redactor: Redactor,
        *,
        cloudwatch_client: Any = None,
        sns_client: Any = None,
        retry_delays: tuple[float, ...] = (0.5, 1.0),
        on_failure: Callable[[Notification], None] | None = None,
    ) -> None:
        self._settings = settings
        self._redactor = redactor
        self._retry_delays = retry_delays
        self._on_failure = on_failure
        self._dryrun = DryRunWriter(settings.DRYRUN_PATH)
        self.queue: asyncio.Queue[Notification] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0
        self.failed = 0
        self.sent_cloudwatch = 0
        self.sent_sns = 0

        self._cloudwatch: CloudWatchSink | None = None
        self._sns: SnsSink | None = None
        self.mode = "dryrun"
        if settings.AWS_ENABLED:
            self._setup_aws(cloudwatch_client, sns_client)

    def _setup_aws(self, cloudwatch_client: Any, sns_client: Any) -> None:
        s = self._settings
        injected = cloudwatch_client is not None or sns_client is not None
        if not injected and not aws_credentials_available(s.AWS_REGION):
            logger.warning("AWS_ENABLED is true but no credentials were found: dry run only")
            return
        if not injected:
            import boto3

            session = boto3.Session(region_name=s.AWS_REGION)
            cloudwatch_client = session.client("logs")
            sns_client = session.client("sns") if s.SNS_TOPIC_ARN else None
        if cloudwatch_client is not None:
            self._cloudwatch = CloudWatchSink(cloudwatch_client, s.CW_LOG_GROUP, s.CW_LOG_STREAM)
        if sns_client is not None and s.SNS_TOPIC_ARN:
            self._sns = SnsSink(sns_client, s.SNS_TOPIC_ARN)
        self.mode = "live"

    # ---- producer side (called from the pipeline tick) -----------------------------------

    def submit(
        self, record: dict[str, Any], *, sns: bool = False, subject: str = "", body: str = ""
    ) -> None:
        """Queue a payload. Never blocks, never raises."""
        try:
            self.queue.put_nowait(Notification(record, sns, subject, body))
        except asyncio.QueueFull:
            self.dropped += 1
            logger.error("notification queue full: dropped", extra={"dropped": self.dropped})

    # ---- worker ---------------------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="notifier")

    async def stop(self, flush_timeout: float = 5.0) -> None:
        """Give the worker a moment to drain what is queued, then stop it."""
        if self._task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.queue.join(), flush_timeout)
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            batch = [await self.queue.get()]
            while len(batch) < CW_BATCH:
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await asyncio.to_thread(self.deliver, batch)
            except Exception as exc:  # the worker must outlive any single failure
                logger.error("notifier batch failed", extra={"error": type(exc).__name__})
            finally:
                for _ in batch:
                    self.queue.task_done()

    def _clean(self, item: Notification) -> Notification:
        """Defence in depth: the payload was redacted at source; redact the text again anyway."""
        scrub = lambda text: self._redactor.redact(text, track=False)[0]  # noqa: E731
        record = {k: scrub(v) if isinstance(v, str) else v for k, v in item.record.items()}
        return Notification(record, item.sns, scrub(item.subject), scrub(item.body))

    def deliver(self, batch: list[Notification]) -> None:
        """Deliver one batch (runs in a worker thread). Public so tests can call it directly."""
        items = [self._clean(i) for i in batch]
        for item in items:  # the local copy comes first: it works with no AWS at all
            try:
                self._dryrun.write(item.record)
            except OSError as exc:
                logger.error("dry-run write failed", extra={"error": type(exc).__name__})
        if self._cloudwatch is not None:
            sink = self._cloudwatch
            if self._with_retries(lambda: sink.put([i.record for i in items]), "cloudwatch", items):
                self.sent_cloudwatch += len(items)
        if self._sns is not None:
            topic = self._sns
            for item in (i for i in items if i.sns):
                if self._with_retries(
                    lambda item=item: topic.publish(item.subject, item.body), "sns", [item]
                ):
                    self.sent_sns += 1

    def _with_retries(
        self, call: Callable[[], None], channel: str, items: list[Notification]
    ) -> bool:
        """Try ``call`` (with backoff between attempts). True if it went through."""
        for attempt in range(len(self._retry_delays) + 1):
            try:
                call()
                return True
            except Exception as exc:
                logger.warning(
                    "aws call failed",
                    extra={"channel": channel, "attempt": attempt + 1, "error": type(exc).__name__},
                )
                if attempt < len(self._retry_delays):
                    time.sleep(self._retry_delays[attempt])
        self.failed += len(items)
        for item in items:
            if self._on_failure is not None:
                self._on_failure(item)
        return False
