import re

import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def _no_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)


def test_defaults_match_spec() -> None:
    s = Settings()
    assert s.LOG_PATH == "data/logs/app.log"
    assert (s.TICK_SECONDS, s.WINDOW_SECONDS) == (1, 60)
    assert (s.BASELINE_HISTORY_SAMPLES, s.WARMUP_SAMPLES, s.MIN_VOLUME) == (900, 30, 30)
    assert (s.OPEN_Z, s.RESOLVE_Z) == (3.0, 1.5)
    assert (s.PERSIST_TICKS, s.RESOLVE_TICKS) == (5, 30)
    assert s.ABS_FLOOR_ERROR == 0.02
    assert (s.COOLDOWN_SECONDS, s.CORRELATION_WINDOW_SECONDS) == (60, 180)
    assert s.AWS_ENABLED is False
    assert s.AWS_REGION == "us-east-1"
    assert (s.CW_LOG_GROUP, s.CW_LOG_STREAM) == ("/sentinel-ai/incidents", "demo")
    assert s.SNS_TOPIC_ARN == ""
    assert (s.LLM_PROVIDER, s.LLM_MODEL) == ("none", "")
    assert s.GENERATOR_ENABLED is True


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPEN_Z", "4.5")
    monkeypatch.setenv("AWS_ENABLED", "true")
    s = Settings()
    assert s.OPEN_Z == 4.5
    assert s.AWS_ENABLED is True


def test_default_log_regex_parses_both_shapes() -> None:
    rx = re.compile(Settings().LOG_REGEX)
    with_latency = rx.match(
        "2026-09-28T10:00:02.004Z ERROR appointment-service latency_ms=3004 "
        "Database timeout while saving appointment 88214"
    )
    assert with_latency and with_latency["latency_ms"] == "3004"
    assert with_latency["service"] == "appointment-service"
    without = rx.match("2026-09-28T10:00:01.123Z INFO  database Checkpoint complete")
    assert without and without["latency_ms"] is None
    assert without["message"] == "Checkpoint complete"
