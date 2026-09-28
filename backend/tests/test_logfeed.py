"""The live log feed: ring buffer, per-tick sampling and caps, /api/logs/recent, PHI safety."""

import json

import pytest
from fastapi.testclient import TestClient
from helpers import LineRig, all_services_feeds, make_event, make_settings
from test_api import client_for
from test_pipeline_e2e import PHI, PHI_ERROR_LINE

from app.logfeed import LogFeed
from app.models import Level


def feed_of(**settings: int) -> LogFeed:
    return LogFeed(make_settings(**settings))


def add(feed: LogFeed, n: int, *, error: bool = False, warn: bool = False, tag: str = "x") -> None:
    for i in range(n):
        feed.add(make_event("database", f"{tag} {i}", error=error, warn=warn))


def tags(batch: dict) -> list[str]:
    return [line["message"] for line in batch["lines"]]


# ---- per-tick batches -------------------------------------------------------------------


def test_an_empty_tick_still_produces_a_batch() -> None:
    assert feed_of().flush() == {"lines": [], "sampled_out": 0, "capped": 0}


def test_every_error_and_warn_line_goes_out_and_info_is_sampled() -> None:
    feed = feed_of()
    add(feed, 4, error=True, tag="err")
    add(feed, 3, warn=True, tag="warn")
    add(feed, 100, tag="info")
    batch = feed.flush()
    levels = [line["level"] for line in batch["lines"]]
    assert levels.count("ERROR") == 4 and levels.count("WARN") == 3
    assert levels.count("INFO") == 10  # LOG_INFO_PER_TICK
    assert batch["sampled_out"] == 90 and batch["capped"] == 0
    assert len(batch["lines"]) == 17


def test_the_info_sample_is_spread_through_the_tick_and_in_arrival_order() -> None:
    feed = feed_of()
    add(feed, 100, tag="info")
    batch = feed.flush()
    numbers = [int(m.split()[1]) for m in tags(batch)]
    assert numbers == sorted(numbers) and len(numbers) == 10
    assert numbers[0] == 0 and numbers[-1] >= 80  # not just the first ten


def test_a_quiet_tick_sends_all_its_info_lines() -> None:
    feed = feed_of()
    add(feed, 7, tag="info")
    batch = feed.flush()
    assert len(batch["lines"]) == 7 and batch["sampled_out"] == 0


def test_error_lines_are_never_sampled_out() -> None:
    feed = feed_of()
    for _ in range(30):  # interleave errors among plenty of INFO noise
        add(feed, 20, tag="info")
        add(feed, 1, error=True, tag="err")
    batch = feed.flush()
    assert sum(line["level"] == "ERROR" for line in batch["lines"]) == 30
    assert batch["capped"] == 0
    assert batch["sampled_out"] == 600 - 10


def test_critical_counts_as_an_error_and_debug_as_info() -> None:
    feed = feed_of()
    event = make_event("database", "boom", error=True)
    feed.add(event.model_copy(update={"level": Level.CRITICAL}))
    feed.add(make_event("database", "d").model_copy(update={"level": Level.DEBUG}))
    batch = feed.flush()
    assert {line["level"] for line in batch["lines"]} == {"CRITICAL", "DEBUG"}


def test_the_message_cap_holds_and_errors_win_over_warns_and_info() -> None:
    feed = feed_of()
    add(feed, 20, warn=True, tag="warn")
    add(feed, 45, error=True, tag="err")
    add(feed, 30, tag="info")
    batch = feed.flush()
    levels = [line["level"] for line in batch["lines"]]
    assert len(batch["lines"]) == 50  # LOG_LINES_PER_MESSAGE
    assert levels.count("ERROR") == 45  # all errors survive...
    assert levels.count("WARN") == 5 and levels.count("INFO") == 0  # ...the rest is squeezed
    assert batch["capped"] == 15 and batch["sampled_out"] == 30


def test_more_errors_than_the_cap_keeps_the_newest_and_reports_the_rest() -> None:
    feed = feed_of()
    add(feed, 80, error=True, tag="err")
    batch = feed.flush()
    numbers = [int(m.split()[1]) for m in tags(batch)]
    assert numbers == list(range(30, 80))  # newest 50, still in arrival order
    assert batch["capped"] == 30 and batch["sampled_out"] == 0


def test_caps_are_configurable() -> None:
    feed = feed_of(LOG_INFO_PER_TICK=2, LOG_LINES_PER_MESSAGE=4)
    add(feed, 50, tag="info")
    assert len(feed.flush()["lines"]) == 2
    add(feed, 9, error=True)
    batch = feed.flush()
    assert len(batch["lines"]) == 4 and batch["capped"] == 5


def test_each_flush_covers_only_its_own_tick() -> None:
    feed = feed_of()
    add(feed, 3, tag="first")
    feed.flush()
    add(feed, 2, tag="second")
    assert {m.split()[0] for m in tags(feed.flush())} == {"second"}
    assert feed.flush()["lines"] == []


# ---- the ring buffer --------------------------------------------------------------------


def test_the_buffer_keeps_the_last_500_events_newest_last() -> None:
    feed = feed_of()
    add(feed, 700, tag="n")
    lines = feed.recent(limit=1000)
    assert len(lines) == 500
    assert lines[0].message == "n 200" and lines[-1].message == "n 699"


def test_recent_filters_before_it_limits() -> None:
    feed = feed_of()
    feed.add(make_event("auth-service", "old err", error=True))
    add(feed, 50, tag="db")
    assert [line.message for line in feed.recent("auth-service", limit=5)] == ["old err"]
    assert [line.level for line in feed.recent(levels={Level.ERROR})] == [Level.ERROR]
    assert len(feed.recent(limit=7)) == 7 and feed.recent(limit=0) == []


def test_a_line_carries_exactly_the_contract_fields() -> None:
    feed = feed_of()
    add(feed, 1, error=True, tag="Timeout after 30 ms")
    (line,) = feed.flush()["lines"]
    assert set(line) == {"ts", "level", "service", "message", "pattern_id"}
    assert line["pattern_id"]
    feed.add(make_event("database", "fine"))
    (info,) = feed.flush()["lines"]
    assert info["pattern_id"] is None


# ---- through the real pipeline ----------------------------------------------------------


def test_the_pipeline_publishes_one_logs_message_per_tick() -> None:
    rig = LineRig(*all_services_feeds(lines=6))
    rig.run(5)
    assert len(rig.of_type("logs")) == 5
    for _, message in rig.messages:
        assert message["type"] in {"metrics", "logs"}
    data = rig.of_type("logs")[-1]
    assert set(data) == {"lines", "sampled_out", "capped"}
    assert len(data["lines"]) <= 50 and data["sampled_out"] > 0
    assert rig.pipeline.errors == {}


def test_unparsed_lines_are_fed_as_unknown_info() -> None:
    rig = LineRig()
    messages = rig.tick(extra_lines=["no format at all"])
    (line,) = next(m["data"] for m in messages if m["type"] == "logs")["lines"]
    assert (line["service"], line["level"], line["message"]) == (
        "unknown", "INFO", "no format at all",
    )  # fmt: skip


def test_a_failing_logs_stage_is_counted_and_the_tick_carries_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = LineRig(*all_services_feeds(lines=2))
    monkeypatch.setattr(rig.pipeline.logfeed, "flush", lambda: 1 / 0)
    messages = rig.tick()
    assert rig.pipeline.errors["logs"] == 1
    assert [m["type"] for m in messages] == ["metrics"]


PHI_INFO_LINE = (
    "2026-09-28T10:00:01.000Z INFO  appointment-service latency_ms=90 Booked for "
    'member_id=MBR7654321 ssn=987-65-4321 email=john.smith@example.com patient_name="John Smith"'
)
PHI_JSON_LINE = json.dumps(
    {
        "ts": "2026-09-28T10:00:01.000Z", "level": "ERROR", "service": "claims-service",
        "msg": "Claim rejected for ssn=111-22-3333 and maria.garcia@example.com",
    }
)  # fmt: skip
PHI_UNPARSED_LINE = '??? patient_name="Aisha Khan" ssn=222-33-4444 email=aisha.khan@example.com'


def test_no_log_message_or_buffer_entry_carries_phi() -> None:
    rig = LineRig(*all_services_feeds(lines=3))
    phi_lines = [
        PHI_ERROR_LINE.format(ts="2026-09-28T10:00:01.000Z").rstrip("\n"),
        PHI_INFO_LINE,
        PHI_JSON_LINE,
        PHI_UNPARSED_LINE,
    ]
    assert all(PHI.findall(line) for line in phi_lines[:3])  # the scan means something
    for _ in range(20):
        rig.tick(extra_lines=phi_lines)

    sent = json.dumps(rig.of_type("logs"))
    buffered = json.dumps(
        [line.model_dump(mode="json") for line in rig.pipeline.logfeed.recent(limit=500)]
    )
    api = json.dumps(client_for(rig).get("/api/logs/recent?limit=500").json())
    for name, text in {"ws messages": sent, "buffer": buffered, "api": api}.items():
        assert PHI.findall(text) == [], f"PHI leaked into the {name}"
        assert "987-65-4321" not in text and "111-22-3333" not in text, name
        assert "222-33-4444" not in text and "aisha.khan" not in text, name
    assert "member_id=[REDACTED]" in sent  # the PHI really did pass through these lines
    assert rig.pipeline.redactor.total > 0


# ---- GET /api/logs/recent ---------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    rig = LineRig(*all_services_feeds(lines=5, p_error=0.3), store=False)
    rig.run(20)  # 7 services x 5 lines x 20 ticks: more than the 500-line buffer holds
    return client_for(rig)


def test_recent_returns_the_buffer_newest_last(client: TestClient) -> None:
    lines = client.get("/api/logs/recent?limit=500").json()
    assert len(lines) == 500
    assert set(lines[0]) == {"ts", "level", "service", "message", "pattern_id"}
    assert len(client.get("/api/logs/recent").json()) == 200  # the default limit
    tail = client.get("/api/logs/recent?limit=3").json()
    assert tail == lines[-3:]


def test_recent_filters_by_service_and_level(client: TestClient) -> None:
    db = client.get("/api/logs/recent?service=database&limit=500").json()
    assert db and {line["service"] for line in db} == {"database"}
    errors = client.get("/api/logs/recent?level=error&limit=500").json()
    assert errors and {line["level"] for line in errors} == {"ERROR"}
    assert all(line["pattern_id"] for line in errors)
    both = client.get("/api/logs/recent?level=ERROR,%20info&service=claims-service").json()
    assert {line["level"] for line in both} == {"ERROR", "INFO"}
    assert {line["service"] for line in both} == {"claims-service"}
    assert client.get("/api/logs/recent?service=nobody").json() == []
    assert client.get("/api/logs/recent?service=&level=").status_code == 200  # empty = no filter


def test_recent_accepts_the_level_aliases(client: TestClient) -> None:
    assert client.get("/api/logs/recent?level=WARNING").status_code == 200
    assert client.get("/api/logs/recent?level=FATAL").status_code == 200


@pytest.mark.parametrize("query", ["level=LOUD", "level=ERROR,LOUD", "limit=0", "limit=501"])
def test_recent_rejects_bad_parameters(client: TestClient, query: str) -> None:
    assert client.get(f"/api/logs/recent?{query}").status_code == 422
