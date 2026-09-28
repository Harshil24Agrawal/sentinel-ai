"""Notifier: a queue and a worker, so AWS latency or failure can never block the pipeline."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import boto3

from app.config import Settings
from app.notifier.cloudwatch import CloudWatchSink
from app.notifier.dryrun import DryRunWriter
from app.notifier.sns import SnsSink
from app.redaction import Redactor

logger = logging.getLogger(__name__)

QUEUE_SIZE = 2000
CW_BATCH = 25
DELIVERY_LOG_SIZE = 10


@dataclass(slots=True)
class Notification:
    """One outgoing payload. ``sns`` marks the ones that also go to SNS (HIGH+ only)."""

    record: dict[str, Any]
    sns: bool = False
    subject: str = ""
    body: str = ""
    attempts: int = field(default=0)


def _error_str(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


class Notifier:
    """Always writes the dry-run JSONL. Sends to CloudWatch (all state changes) and SNS (the
    important ones) only when ``AWS_ENABLED`` is true AND the startup self-check passes.

    ``submit`` never blocks and never raises: a full queue drops the item and counts it; the
    worker retries AWS calls with backoff off the event loop and gives up quietly.

    ``mode`` is one of ``"live"`` (at least one AWS sink is up), ``"dryrun"`` (AWS disabled, or
    enabled but nothing configured/working past credentials), or ``"error"`` (credentials
    themselves failed the self-check). ``aws_checks`` holds the per-check {ok, error} detail
    that ``GET /api/health`` reports, so a failed check is visible without reading logs.
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
        self.delivery_log: deque[dict[str, Any]] = deque(maxlen=DELIVERY_LOG_SIZE)

        self._cloudwatch: CloudWatchSink | None = None
        self._sns: SnsSink | None = None
        self.mode = "dryrun"
        self.aws_checks: dict[str, dict[str, Any]] = {
            "credentials": {"ok": False, "error": None},
            "cloudwatch": {"ok": False, "error": None},
            "sns": {"ok": False, "error": None},
        }
        if settings.AWS_ENABLED:
            self._setup_aws(cloudwatch_client, sns_client)

    def _setup_aws(self, cloudwatch_client: Any, sns_client: Any) -> None:
        """Build the AWS sinks. Test doubles (``cloudwatch_client``/``sns_client`` injected)
        skip the real startup self-check and go straight to lazy create-on-first-use, so unit
        tests never touch the network. A real boto3 session runs the full self-check: identity,
        then the log group/stream, then the SNS topic, each independently — one failing does
        not stop the others from going live."""
        s = self._settings
        if cloudwatch_client is not None or sns_client is not None:
            if cloudwatch_client is not None:
                self._cloudwatch = CloudWatchSink(
                    cloudwatch_client, s.CW_LOG_GROUP, s.CW_LOG_STREAM
                )
            if sns_client is not None and s.SNS_TOPIC_ARN:
                self._sns = SnsSink(sns_client, s.SNS_TOPIC_ARN)
            self.mode = "live"
            self.aws_checks = {
                "credentials": {"ok": True, "error": None},
                "cloudwatch": {"ok": self._cloudwatch is not None, "error": None},
                "sns": {"ok": self._sns is not None or not s.SNS_TOPIC_ARN, "error": None},
            }
            return

        checks: dict[str, dict[str, Any]] = {
            "credentials": {"ok": False, "error": None},
            "cloudwatch": {"ok": False, "error": None},
            "sns": {"ok": False, "error": None},
        }
        try:
            session = boto3.Session(profile_name=s.AWS_PROFILE or None, region_name=s.AWS_REGION)
            identity = session.client("sts").get_caller_identity()
            checks["credentials"] = {"ok": True, "error": None, "arn": identity.get("Arn")}
        except Exception as exc:
            checks["credentials"]["error"] = _error_str(exc)
            checks["cloudwatch"]["error"] = "skipped: credentials check failed"
            checks["sns"]["error"] = "skipped: credentials check failed"
            logger.warning("AWS credentials check failed: dry run only", extra={"error": str(exc)})
            self.aws_checks = checks
            self.mode = "error"
            return

        sink = CloudWatchSink(session.client("logs"), s.CW_LOG_GROUP, s.CW_LOG_STREAM)
        try:
            sink.ensure()
            checks["cloudwatch"] = {"ok": True, "error": None}
            self._cloudwatch = sink
        except Exception as exc:
            checks["cloudwatch"] = {"ok": False, "error": _error_str(exc)}
            logger.warning("CloudWatch self-check failed", extra={"error": str(exc)})

        if s.SNS_TOPIC_ARN:
            sns = session.client("sns")
            try:
                sns.get_topic_attributes(TopicArn=s.SNS_TOPIC_ARN)
                checks["sns"] = {"ok": True, "error": None}
                self._sns = SnsSink(sns, s.SNS_TOPIC_ARN)
            except Exception as exc:
                checks["sns"] = {"ok": False, "error": _error_str(exc)}
                logger.warning("SNS self-check failed", extra={"error": str(exc)})
        else:
            checks["sns"] = {"ok": True, "error": None}  # not configured; not a failure

        self.aws_checks = checks
        self.mode = "live" if (self._cloudwatch is not None or self._sns is not None) else "dryrun"

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
        started = time.monotonic()
        ok = True
        for item in items:  # the local copy comes first: it works with no AWS at all
            try:
                self._dryrun.write(item.record)
            except OSError as exc:
                ok = False
                logger.error("dry-run write failed", extra={"error": type(exc).__name__})
        self._log_delivery("dryrun", self._batch_severity(items), ok, started)
        if self._cloudwatch is not None:
            sink = self._cloudwatch
            started = time.monotonic()
            success = self._with_retries(
                lambda: sink.put([i.record for i in items]), "cloudwatch", items
            )
            if success:
                self.sent_cloudwatch += len(items)
            self._log_delivery("cloudwatch", self._batch_severity(items), success, started)
        if self._sns is not None:
            topic = self._sns
            for item in (i for i in items if i.sns):
                started = time.monotonic()
                success = self._with_retries(
                    lambda item=item: topic.publish(item.subject, item.body), "sns", [item]
                )
                if success:
                    self.sent_sns += 1
                self._log_delivery("sns", item.record.get("severity"), success, started)

    @staticmethod
    def _batch_severity(items: list[Notification]) -> str | None:
        severities = [i.record.get("severity") for i in items if i.record.get("severity")]
        return severities[-1] if severities else None

    def _log_delivery(self, channel: str, severity: str | None, ok: bool, started: float) -> None:
        self.delivery_log.append(
            {
                "ts": time.time(),
                "channel": channel,
                "severity": severity,
                "ok": ok,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
            }
        )

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

    # ---- POST /api/aws/test ----------------------------------------------------------------

    def send_test(self) -> dict[str, Any]:
        """Send one clearly-labeled test message to CloudWatch and SNS, synchronously (runs in
        a worker thread via ``asyncio.to_thread``). Returns each channel's result; never raises."""
        now = time.time()
        record = {
            "event": "test",
            "incident_id": "test",
            "group_id": "test",
            "service": "sentinelai",
            "severity": "LOW",
            "origin": "sentinelai",
            "error_rate": None,
            "baseline": None,
            "z": None,
            "timestamp": now,
            "channel": "test",
            "message": "SentinelAI connectivity test — safe to ignore",
        }
        with contextlib.suppress(OSError):
            self._dryrun.write(record)

        result: dict[str, Any] = {
            "cloudwatch": {"ok": False, "error": f"not configured (mode: {self.mode})"},
            "sns": {"ok": False, "error": f"not configured (mode: {self.mode})"},
        }
        if self._cloudwatch is not None:
            started = time.monotonic()
            try:
                self._cloudwatch.put([record])
                result["cloudwatch"] = {"ok": True, "error": None}
            except Exception as exc:
                result["cloudwatch"] = {"ok": False, "error": _error_str(exc)}
            self._log_delivery("cloudwatch", "LOW", result["cloudwatch"]["ok"], started)
        if self._sns is not None:
            started = time.monotonic()
            try:
                self._sns.publish(
                    "[TEST] SentinelAI connectivity check",
                    "This is a test message from SentinelAI confirming SNS delivery works. "
                    "Safe to ignore.",
                )
                result["sns"] = {"ok": True, "error": None}
            except Exception as exc:
                result["sns"] = {"ok": False, "error": _error_str(exc)}
            self._log_delivery("sns", "LOW", result["sns"]["ok"], started)
        return result
