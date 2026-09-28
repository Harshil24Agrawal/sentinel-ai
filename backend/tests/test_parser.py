import json
from datetime import UTC, datetime

import pytest

from app.clock import FakeClock
from app.models import Level
from app.parser import LogParser
from app.redaction import REDACTED, Redactor

LINE = (
    "2026-09-28T10:00:02.004Z ERROR appointment-service latency_ms=3004 "
    "Database timeout while saving appointment 88214"
)
EPOCH = datetime(2026, 9, 28, 10, 0, 2, 4000, tzinfo=UTC).timestamp()


@pytest.fixture
def redactor() -> Redactor:
    return Redactor()


@pytest.fixture
def parser(clock: FakeClock, redactor: Redactor) -> LogParser:
    return LogParser(clock, redactor=redactor)


def test_parses_primary_format(parser: LogParser) -> None:
    ev = parser.parse(LINE)
    assert ev.parsed
    assert ev.ts == pytest.approx(EPOCH)
    assert ev.level is Level.ERROR and ev.is_error and not ev.is_warn
    assert ev.service == "appointment-service"
    assert ev.latency_ms == 3004
    assert ev.message == "Database timeout while saving appointment 88214"
    assert ev.template == "Database timeout while saving appointment <NUM>"
    assert ev.pattern_id is not None


def test_latency_is_optional_and_padding_is_tolerated(parser: LogParser) -> None:
    ev = parser.parse(
        "2026-09-28T10:00:01.123Z INFO  appointment-service Appointment request 88213 completed"
    )
    assert ev.level is Level.INFO
    assert ev.latency_ms is None
    assert ev.message == "Appointment request 88213 completed"


@pytest.mark.parametrize(
    ("word", "level", "is_error", "is_warn"),
    [
        ("DEBUG", Level.DEBUG, False, False),
        ("INFO", Level.INFO, False, False),
        ("WARN", Level.WARN, False, True),
        ("WARNING", Level.WARN, False, True),
        ("ERROR", Level.ERROR, True, False),
        ("CRITICAL", Level.CRITICAL, True, False),
        ("FATAL", Level.CRITICAL, True, False),
        ("warning", Level.WARN, False, True),
    ],
)
def test_level_mapping(
    parser: LogParser, word: str, level: Level, is_error: bool, is_warn: bool
) -> None:
    ev = parser.parse(f"2026-09-28T10:00:01Z {word} svc something happened 7")
    assert (ev.level, ev.is_error, ev.is_warn) == (level, is_error, is_warn)


def test_patterns_only_for_error_and_warn_lines(parser: LogParser) -> None:
    info = parser.parse("2026-09-28T10:00:01Z INFO svc all good 7")
    warn = parser.parse("2026-09-28T10:00:01Z WARN svc slow thing 7")
    assert info.pattern_id is None and info.template is None
    assert warn.template == "slow thing <NUM>"


def test_parses_json_lines(parser: LogParser) -> None:
    raw = json.dumps(
        {"ts": "2026-09-28T10:00:02.004Z", "level": "error", "service": "database",
         "latency_ms": 3004, "msg": "Connection pool exhausted after 3004 ms"}
    )  # fmt: skip
    ev = parser.parse(raw)
    assert ev.parsed and ev.service == "database" and ev.is_error
    assert ev.latency_ms == 3004
    assert ev.ts == pytest.approx(EPOCH)
    assert ev.template == "Connection pool exhausted after <NUM> ms"


def test_json_with_epoch_timestamps(parser: LogParser) -> None:
    secs = parser.parse('{"ts": 1790000000, "level": "INFO", "service": "a", "msg": "x"}')
    millis = parser.parse('{"ts": 1790000000123, "level": "INFO", "service": "a", "msg": "x"}')
    assert secs.ts == 1790000000
    assert millis.ts == pytest.approx(1790000000.123)


def test_missing_or_unparsable_timestamp_uses_clock(parser: LogParser, clock: FakeClock) -> None:
    ev = parser.parse('{"level": "INFO", "service": "a", "msg": "no timestamp"}')
    assert ev.parsed and ev.ts == clock.now()
    ev = parser.parse("2026-99-99T99:99:99Z INFO svc bad date 1")
    assert ev.parsed and ev.ts == clock.now()


@pytest.mark.parametrize(
    "junk",
    [
        "",
        "   ",
        "just some text",
        "2026-09-28T10:00:01Z",
        "2026-09-28T10:00:01Z NOTALEVEL svc hello",
        "{",
        "{}",
        '{"level": "INFO"}',
        "[1, 2]",
        '{"level": "INFO", "service": "", "msg": "x"}',
        "\x00\xff\xfe garbage",
    ],
)
def test_bad_lines_are_unparsed_never_a_crash(parser: LogParser, junk: str) -> None:
    ev = parser.parse(junk)
    assert not ev.parsed
    assert ev.service == "unknown"
    assert not ev.is_error and ev.pattern_id is None
    assert parser.unparsed == 1


def test_unparsed_counter_counts_only_failures(parser: LogParser) -> None:
    parser.parse(LINE)
    parser.parse("nonsense")
    parser.parse("more nonsense")
    assert parser.unparsed == 2


def test_unparsed_line_is_kept_but_redacted(parser: LogParser) -> None:
    ev = parser.parse("weird format ssn=123-45-6789 for jane@example.com")
    assert not ev.parsed
    assert "123-45-6789" not in ev.raw and "example.com" not in ev.raw
    assert REDACTED in ev.raw and ev.message == ev.raw


def test_phi_is_redacted_in_message_and_raw_and_counted_once(
    parser: LogParser, redactor: Redactor
) -> None:
    ev = parser.parse(
        "2026-09-28T10:00:01Z INFO appointment-service Appointment request 88213 completed "
        'member_id=MBR1234567 patient_name="Jane Doe"'
    )
    for text in (ev.message, ev.raw):
        assert "MBR1234567" not in text and "Jane" not in text
        assert f"member_id={REDACTED}" in text
    assert ev.message.endswith(f"patient_name={REDACTED}")
    assert redactor.total == 2  # two PHI fields, not four


def test_phi_in_error_line_is_redacted_before_templating(parser: LogParser) -> None:
    a = parser.parse("2026-09-28T10:00:01Z ERROR svc Lookup failed for member_id=MBR1111111")
    b = parser.parse("2026-09-28T10:00:02Z ERROR svc Lookup failed for member_id=MBR2222222")
    assert a.template == "Lookup failed for member_id=[REDACTED]"
    assert a.pattern_id == b.pattern_id


def test_json_phi_is_redacted_in_raw(parser: LogParser) -> None:
    ev = parser.parse(
        '{"ts": 1790000000, "level": "INFO", "service": "a", "msg": "ok", "ssn": "123-45-6789"}'
    )
    assert "123-45-6789" not in ev.raw


def test_very_long_lines_are_capped_after_redaction(parser: LogParser) -> None:
    ev = parser.parse("2026-09-28T10:00:01Z INFO svc " + "x" * 10_000)
    assert len(ev.message) <= 2000 and len(ev.raw) <= 2000


def test_trailing_newline_and_carriage_return_are_stripped(parser: LogParser) -> None:
    assert parser.parse("2026-09-28T10:00:01Z INFO svc hello\r\n").message == "hello"
