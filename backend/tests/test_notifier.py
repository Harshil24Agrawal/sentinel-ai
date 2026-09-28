import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from helpers import make_settings

from app.notifier import dispatcher
from app.notifier.dispatcher import Notification, Notifier
from app.redaction import Redactor


class AlreadyExistsError(Exception):
    """Stands in for botocore's ResourceAlreadyExistsException."""


class FakeLogs:
    def __init__(self, group_exists: bool = False, fail_puts: int = 0) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.group_exists = group_exists
        self.fail_puts = fail_puts

    def create_log_group(self, **kw: Any) -> None:
        self.calls.append(("create_log_group", kw))
        if self.group_exists:
            raise AlreadyExistsError("ResourceAlreadyExistsException")

    def create_log_stream(self, **kw: Any) -> None:
        self.calls.append(("create_log_stream", kw))

    def put_log_events(self, **kw: Any) -> None:
        self.calls.append(("put_log_events", kw))
        if self.fail_puts:
            self.fail_puts -= 1
            raise RuntimeError("throttled")


class FakeSns:
    def __init__(self, fail_times: int = 0, gate: threading.Event | None = None) -> None:
        self.published: list[dict[str, Any]] = []
        self.attempts = 0
        self.fail_times = fail_times
        self.gate = gate

    def publish(self, **kw: Any) -> None:
        self.attempts += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.attempts <= self.fail_times:
            raise RuntimeError("SNS unavailable")
        self.published.append(kw)


def make_notifier(tmp_path: Path, *, aws: bool = False, **kwargs: Any) -> Notifier:
    settings = make_settings(
        DRYRUN_PATH=str(tmp_path / "notifications.jsonl"),
        AWS_ENABLED=aws,
        SNS_TOPIC_ARN="arn:aws:sns:us-east-1:123456789012:sentinel",
        CW_LOG_GROUP="/sentinel-ai/incidents",
        CW_LOG_STREAM="test",
    )
    return Notifier(
        settings, Redactor(), retry_delays=kwargs.pop("retry_delays", (0.0, 0.0)), **kwargs
    )


def lines(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "notifications.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def state_change(ts: float = 100.0) -> Notification:
    return Notification(
        {"ts": ts, "channel": "cloudwatch", "type": "incident_updated", "group_id": "g1"}
    )


def sns_notification(
    ts: float = 101.0, subject: str = "[SentinelAI] CRITICAL: database incident"
) -> Notification:
    record = {
        "ts": ts,
        "channel": "sns",
        "type": "notification",
        "group_id": "g1",
        "severity": "CRITICAL",
    }
    return Notification(record, sns=True, subject=subject, body="body text")


# ---- the local copy ---------------------------------------------------------------------


def test_every_payload_is_written_to_the_dry_run_file(tmp_path: Path) -> None:
    n = make_notifier(tmp_path)
    n.deliver([state_change(1.0), sns_notification(2.0)])
    n.deliver([state_change(3.0)])
    written = lines(tmp_path)
    assert [r["ts"] for r in written] == [1.0, 2.0, 3.0]
    assert [r["channel"] for r in written] == ["cloudwatch", "sns", "cloudwatch"]
    assert n.mode == "dryrun"


def test_payloads_are_redacted_again_before_they_are_written(tmp_path: Path) -> None:
    n = make_notifier(tmp_path)
    dirty = Notification(
        {"ts": 1.0, "channel": "sns", "body": "lookup failed member_id=MBR1234567 ssn=123-45-6789"},
        sns=True,
        subject="x",
        body="email=jane@example.com",
    )
    n.deliver([dirty])
    text = (tmp_path / "notifications.jsonl").read_text(encoding="utf-8")
    assert "MBR1234567" not in text and "123-45-6789" not in text and "jane@example.com" not in text
    assert "member_id=[REDACTED]" in text


# ---- AWS only when enabled and credentialed ---------------------------------------------


def test_no_aws_calls_unless_enabled(tmp_path: Path) -> None:
    logs, sns = FakeLogs(), FakeSns()
    n = make_notifier(tmp_path, aws=False, cloudwatch_client=logs, sns_client=sns)
    n.deliver([state_change(), sns_notification()])
    assert logs.calls == [] and sns.published == []
    assert n.mode == "dryrun" and len(lines(tmp_path)) == 2


def test_enabled_without_credentials_falls_back_to_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatcher, "aws_credentials_available", lambda region: False)
    n = make_notifier(tmp_path, aws=True)
    assert n.mode == "dryrun"
    n.deliver([sns_notification()])  # still works, locally
    assert len(lines(tmp_path)) == 1


def test_live_mode_sends_state_changes_to_cloudwatch_and_only_notifications_to_sns(
    tmp_path: Path,
) -> None:
    logs, sns = FakeLogs(), FakeSns()
    n = make_notifier(tmp_path, aws=True, cloudwatch_client=logs, sns_client=sns)
    assert n.mode == "live"
    long_subject = "[SentinelAI] CRITICAL: " + "x" * 200 + " — café\nsecond line"
    n.deliver([sns_notification(5.0, long_subject), state_change(4.0)])

    names = [c[0] for c in logs.calls]
    assert names == ["create_log_group", "create_log_stream", "put_log_events"]
    put = logs.calls[-1][1]
    assert put["logGroupName"] == "/sentinel-ai/incidents" and put["logStreamName"] == "test"
    assert [e["timestamp"] for e in put["logEvents"]] == [4000, 5000]  # chronological, in ms
    assert json.loads(put["logEvents"][0]["message"])["type"] == "incident_updated"

    (message,) = sns.published  # the state change did not go to SNS
    assert message["TopicArn"].endswith(":sentinel") and message["Message"] == "body text"
    assert len(message["Subject"]) <= 100 and message["Subject"].isascii()
    assert "\n" not in message["Subject"]
    assert (n.sent_cloudwatch, n.sent_sns, n.failed) == (2, 1, 0)


def test_an_existing_log_group_is_not_an_error(tmp_path: Path) -> None:
    logs = FakeLogs(group_exists=True)
    n = make_notifier(tmp_path, aws=True, cloudwatch_client=logs, sns_client=FakeSns())
    n.deliver([state_change()])
    assert logs.calls[-1][0] == "put_log_events" and n.failed == 0


def test_log_group_and_stream_are_created_once(tmp_path: Path) -> None:
    logs = FakeLogs()
    n = make_notifier(tmp_path, aws=True, cloudwatch_client=logs, sns_client=FakeSns())
    n.deliver([state_change()])
    n.deliver([state_change()])
    assert [c[0] for c in logs.calls].count("create_log_group") == 1


# ---- failures ---------------------------------------------------------------------------


def test_aws_calls_are_retried_with_backoff_then_succeed(tmp_path: Path) -> None:
    sns = FakeSns(fail_times=2)
    n = make_notifier(tmp_path, aws=True, cloudwatch_client=FakeLogs(), sns_client=sns)
    n.deliver([sns_notification()])
    assert sns.attempts == 3 and len(sns.published) == 1  # 3 attempts in all
    assert n.failed == 0 and n.sent_sns == 1


def test_gives_up_quietly_and_the_local_copy_is_still_written(tmp_path: Path) -> None:
    failures: list[Notification] = []
    sns, logs = FakeSns(fail_times=99), FakeLogs(fail_puts=99)
    n = make_notifier(
        tmp_path, aws=True, cloudwatch_client=logs, sns_client=sns, on_failure=failures.append
    )
    n.deliver([sns_notification()])  # must not raise
    assert sns.attempts == 3 and [c[0] for c in logs.calls].count("put_log_events") == 3
    assert n.failed == 2 and len(failures) == 2  # one CloudWatch batch item + one SNS message
    assert len(lines(tmp_path)) == 1  # the dry-run file has it regardless
    assert n.sent_sns == 0


# ---- never blocks the pipeline ----------------------------------------------------------


async def test_a_hanging_aws_call_never_blocks_submit(tmp_path: Path) -> None:
    gate = threading.Event()
    n = make_notifier(
        tmp_path, aws=True, cloudwatch_client=FakeLogs(), sns_client=FakeSns(gate=gate)
    )
    n.start()
    started = time.perf_counter()
    for i in range(50):
        n.submit({"ts": float(i), "channel": "sns"}, sns=True, subject="s", body="b")
    assert time.perf_counter() - started < 0.5  # SNS is stuck, the caller did not notice
    await asyncio.sleep(0.05)  # the worker is now blocked inside the hung call
    n.submit({"ts": 99.0, "channel": "cloudwatch"})  # ...and submitting still works
    gate.set()
    await n.stop(flush_timeout=5)
    assert len(lines(tmp_path)) == 51  # everything was delivered once the call returned


def test_a_full_queue_drops_and_counts_but_never_raises(tmp_path: Path) -> None:
    n = make_notifier(tmp_path)
    n.queue = asyncio.Queue(maxsize=2)
    for i in range(5):
        n.submit({"ts": float(i)})
    assert n.queue.qsize() == 2 and n.dropped == 3


async def test_the_worker_delivers_and_stop_flushes_the_queue(tmp_path: Path) -> None:
    n = make_notifier(tmp_path)
    n.start()
    for i in range(3):
        n.submit({"ts": float(i), "channel": "cloudwatch"})
    await n.stop(flush_timeout=5)
    assert [r["ts"] for r in lines(tmp_path)] == [0.0, 1.0, 2.0]


async def test_the_worker_survives_a_failing_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = make_notifier(tmp_path)
    real = n.deliver
    calls = {"n": 0}

    def flaky(batch: list[Notification]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk on fire")
        real(batch)

    monkeypatch.setattr(n, "deliver", flaky)
    n.start()
    n.submit({"ts": 1.0})
    await asyncio.sleep(0.1)
    n.submit({"ts": 2.0})
    await n.stop(flush_timeout=5)
    assert [r["ts"] for r in lines(tmp_path)] == [2.0]  # the second batch still went through
