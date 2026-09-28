"""The Pipeline itself: messages, statuses, stage isolation, stats. Synthetic traffic, fast."""

import asyncio
import json
from pathlib import Path

import pytest
from helpers import Feed, LineRig, all_services_feeds, make_settings

from app.models import IncidentState, Severity
from app.pipeline import Hub, Pipeline, SimulationDisabledError
from generator.generator import LogGenerator
from generator.scenarios import UnknownScenarioError

WARM = 100
DB_ERROR = "Connection pool exhausted after {n} ms"
APPT_ERROR = "Database timeout while saving appointment {n}"


def feed_of(rig: LineRig, service: str) -> Feed:
    return next(f for f in rig.feeds if f.service == service)


def start_db_outage(rig: LineRig) -> None:
    db, appt = feed_of(rig, "database"), feed_of(rig, "appointment-service")
    db.p_error, db.error_message = 0.40, DB_ERROR
    appt.p_error, appt.error_message = 0.40, APPT_ERROR


def end_outage(rig: LineRig) -> None:
    for name in ("database", "appointment-service"):
        feed = feed_of(rig, name)
        feed.p_error = 0.02
        feed.error_message = "Request {n} failed"  # the outage's own message goes away too


@pytest.fixture(scope="module")
def outage_run() -> LineRig:
    """Warm up, a database outage that spreads to appointment-service, then recovery."""
    rig = LineRig(*all_services_feeds())
    rig.run(WARM)
    start_db_outage(rig)
    rig.run(45)
    end_outage(rig)
    for _ in range(260):
        rig.tick()
        if all(g.state is IncidentState.RESOLVED for g in rig.pipeline.manager.group_summaries()):
            break
    return rig


# ---- messages ---------------------------------------------------------------------------


def test_a_metrics_message_is_published_every_tick(outage_run: LineRig) -> None:
    metrics = [m for _, m in outage_run.messages if m["type"] == "metrics"]
    assert len(metrics) == outage_run.t
    data = metrics[-1]["data"]
    assert set(data) == {"t", "services"}
    assert len(data["services"]) == 7
    entry = data["services"]["database"]
    assert set(entry) == {"volume", "error_rate", "p95_latency", "band", "status"}
    assert len(entry["band"]) == 2 and entry["band"][0] <= entry["band"][1]
    assert [m["data"]["t"] for m in metrics] == sorted(m["data"]["t"] for m in metrics)


def test_one_opened_message_for_the_whole_outage_then_updates_then_one_resolved(
    outage_run: LineRig,
) -> None:
    incident_messages = [
        (tick, m) for tick, m in outage_run.messages if m["type"].startswith("incident_")
    ]
    kinds = [m["type"] for _, m in incident_messages]
    assert kinds[0] == "incident_opened" and kinds.count("incident_opened") == 1
    assert kinds[-1] == "incident_resolved" and kinds.count("incident_resolved") == 1
    assert set(kinds) <= {"incident_opened", "incident_updated", "incident_resolved"}
    assert len({m["data"]["id"] for _, m in incident_messages}) == 1  # one group, one card


def test_incident_messages_carry_the_group_summary(outage_run: LineRig) -> None:
    opened = outage_run.of_type("incident_opened")[0]
    assert opened["state"] == "OPEN" and opened["origin"]
    assert opened["current_severity"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    assert opened["incidents"] and opened["incidents"][0]["timeline"]
    resolved = outage_run.of_type("incident_resolved")[0]
    assert resolved["state"] == "RESOLVED" and resolved["resolved_at"] is not None
    assert resolved["origin"] == "database"
    assert {m["service"] for m in resolved["incidents"]} >= {"database", "appointment-service"}
    assert resolved["max_severity"] in {"HIGH", "CRITICAL"}


def test_notifications_are_dry_run_and_at_most_three(outage_run: LineRig) -> None:
    notes = outage_run.of_type("notification")
    assert 1 <= len(notes) <= 3
    group_id = outage_run.of_type("incident_opened")[0]["id"]
    for note in notes:
        assert set(note) == {"channel", "incident_id", "severity", "ok"}
        assert note["channel"] == "dryrun" and note["ok"] is True
        assert note["incident_id"] == group_id
        assert note["severity"] in {"HIGH", "CRITICAL"}
    log = outage_run.pipeline.notifications
    assert len(log) == len(notes) and log[-1]["kind"] == "resolved"
    assert all("CHECK FIRST:" in n["body"] for n in log if n["kind"] != "resolved")


def test_state_changes_and_notifications_reach_the_notifier(outage_run: LineRig) -> None:
    queued = list(outage_run.pipeline.notifier.queue._queue)  # nothing is draining it here
    sns = [q for q in queued if q.sns]
    state_changes = [q for q in queued if not q.sns]
    assert len(sns) == len(outage_run.of_type("notification"))  # SNS: only the notifications
    assert all(q.subject.startswith("[") and q.subject.isascii() for q in sns)
    assert state_changes and all(q.record["channel"] == "cloudwatch" for q in state_changes)
    assert {q.record["event"] for q in state_changes} >= {"incident_opened", "incident_resolved"}
    # thin CloudWatch: only real state changes, not one "updated" per tick an incident stays open
    assert {q.record["event"] for q in state_changes} <= {
        "incident_opened", "incident_reopened", "incident_escalated", "incident_resolved",
    }  # fmt: skip
    assert len(state_changes) < 30  # the run is 400+ ticks; per-tick noise would dwarf this


def test_the_card_names_the_pattern_and_the_probable_cause(outage_run: LineRig) -> None:
    card = outage_run.pipeline.manager.group_card(outage_run.of_type("incident_opened")[0]["id"])
    assert card is not None
    assert card.origin == "database" and card.has_new_pattern
    templates = {p.template for p in card.patterns}
    assert "Connection pool exhausted after <NUM> ms" in templates
    assert card.hint.startswith("Database or downstream dependency")
    assert card.explanation is not None and "probable origin" in card.explanation.summary


def test_stats_tell_the_alert_fatigue_story(outage_run: LineRig) -> None:
    stats = outage_run.pipeline.stats()
    assert stats.sentinel_incidents == 1
    assert stats.sentinel_notifications == len(outage_run.of_type("notification"))
    assert stats.naive_alerts > 5 * stats.sentinel_notifications
    assert stats.detection_latency_s == {}  # no scenario was started through /simulate
    assert stats.phi_redacted == 0  # this traffic carries no PHI


# ---- statuses ---------------------------------------------------------------------------


def test_service_status_goes_from_warming_up_to_healthy_to_severity() -> None:
    rig = LineRig(*all_services_feeds())
    rig.run(20)
    assert {s.status for s in rig.pipeline.services_view()} == {"warming_up"}
    rig.run(WARM)
    assert {s.status for s in rig.pipeline.services_view()} == {"healthy"}
    start_db_outage(rig)
    rig.run(35)
    statuses = {s.name: s.status for s in rig.pipeline.services_view()}
    assert statuses["database"] in {"MEDIUM", "HIGH", "CRITICAL"}
    assert statuses["appointment-service"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    assert statuses["auth-service"] == "healthy"
    frame = rig.pipeline.latest_metrics()
    assert frame is not None and frame["services"]["database"]["status"] == statuses["database"]


def test_a_service_that_stops_logging_is_silent() -> None:
    rig = LineRig(*all_services_feeds())
    rig.run(WARM)
    feed_of(rig, "notification-service").stop = rig.t
    rig.run(30)
    statuses = {s.name: s.status for s in rig.pipeline.services_view()}
    assert statuses["notification-service"] == "silent"
    assert statuses["database"] == "healthy"


def test_services_view_carries_baseline_and_dependencies() -> None:
    rig = LineRig(*all_services_feeds())
    rig.run(WARM)
    by_name = {s.name: s for s in rig.pipeline.services_view()}
    appointment = by_name["appointment-service"]
    assert appointment.depends_on == ["database", "notification-service"]
    assert appointment.baseline.ready is True
    assert 0.0 <= appointment.baseline.error_rate_median < 0.1
    lo, hi = appointment.baseline.error_rate_band
    assert lo <= appointment.baseline.error_rate_median <= hi
    assert appointment.volume > 500 and appointment.p95_latency is not None


def test_all_known_services_are_listed_even_before_any_traffic() -> None:
    rig = LineRig()
    rig.run(3)
    names = [s.name for s in rig.pipeline.services_view()]
    assert len(names) == 7 and "database" in names
    assert all(s.volume == 0 and s.status == "warming_up" for s in rig.pipeline.services_view())


# ---- history ----------------------------------------------------------------------------


def test_metrics_history_is_bounded_and_filterable() -> None:
    rig = LineRig(*all_services_feeds(lines=2), HISTORY_SECONDS=10)
    rig.run(30)
    frames = rig.pipeline.metrics_history()
    assert len(frames) == 10  # HISTORY_SECONDS / TICK_SECONDS
    assert [f["t"] for f in frames] == sorted(f["t"] for f in frames)  # oldest first
    assert len(rig.pipeline.metrics_history(seconds=3)) == 3
    assert rig.pipeline.metrics_history(seconds=1000) == frames


# ---- resilience -------------------------------------------------------------------------


def test_one_bad_line_never_stops_monitoring() -> None:
    rig = LineRig(*all_services_feeds(lines=4))
    rig.run(5)
    junk = ["", "\x00\xff garbage", "{", "no format at all", "2026-99-99 ??? x y z"]
    rig.tick(extra_lines=junk)
    assert rig.pipeline.aggregator.unparsed == len(junk)
    assert rig.pipeline.errors == {}
    assert rig.pipeline.latest_metrics() is not None  # the tick still completed


def test_a_stage_failure_is_counted_and_the_tick_carries_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = LineRig(*all_services_feeds(lines=4))
    rig.run(5)
    original = rig.pipeline.aggregator.add

    def flaky(event):  # type: ignore[no-untyped-def]
        if "poison" in event.message:
            raise RuntimeError("boom")
        original(event)

    monkeypatch.setattr(rig.pipeline.aggregator, "add", flaky)
    poison = "2026-09-28T10:00:01.000Z INFO database latency_ms=5 poison pill"
    messages = rig.tick(extra_lines=[poison])
    assert rig.pipeline.errors["ingest"] == 1
    assert any(m["type"] == "metrics" for m in messages)
    assert rig.pipeline.aggregator.window("database").volume > 0  # the good lines got through

    def broken_run(frozen):  # type: ignore[no-untyped-def]
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(rig.pipeline.detectors, "run", broken_run)
    messages = rig.tick()
    assert rig.pipeline.errors["detect"] == 1
    assert any(m["type"] == "metrics" for m in messages)  # the dashboard keeps updating
    rig.tick()
    assert rig.pipeline.errors["detect"] == 2  # counted every time, never fatal


def test_errors_are_logged_without_the_offending_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = LineRig(*all_services_feeds(lines=2))
    monkeypatch.setattr(rig.pipeline.aggregator, "add", lambda e: 1 / 0)
    secret = "2026-09-28T10:00:01.000Z INFO database ssn=123-45-6789 secret payload"
    with caplog.at_level("ERROR"):
        rig.tick(extra_lines=[secret])
    text = " ".join(r.getMessage() + json.dumps(r.__dict__, default=str) for r in caplog.records)
    assert "ingest failed" in text and "ZeroDivisionError" in text
    assert "123-45-6789" not in text and "secret payload" not in text


# ---- collector, generator, controls -----------------------------------------------------


def test_lines_written_to_the_log_file_reach_the_pipeline(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text("")
    rig = LineRig(LOG_PATH=str(log))
    assert rig.pipeline.collector.poll_once() == []  # now tailing the (empty) file
    with open(log, "a", encoding="utf-8") as f:
        f.write("2026-09-28T10:00:01.000Z INFO database latency_ms=9 Query 5 completed\n")
        f.write("2026-09-28T10:00:01.500Z ERROR database latency_ms=9 Query 6 aborted\n")
    for line in rig.pipeline.collector.poll_once():
        rig.pipeline.queue.put_nowait(line)
    rig.pipeline.tick_once()  # no argument: drains the collector's queue
    window = rig.pipeline.aggregator.window("database")
    assert (window.volume, window.errors) == (2, 1)
    assert rig.pipeline.queue.empty()


def test_simulate_starts_the_generator_scenario_and_records_it(tmp_path: Path) -> None:
    rig = LineRig(*all_services_feeds())
    generator = LogGenerator(rig.clock, tmp_path / "gen.log", seed=1)
    rig.pipeline.generator = generator
    rig.run(10)
    started = rig.pipeline.simulate("db_degradation")
    assert started == rig.clock.now()
    assert generator.active_scenarios() == ["db_degradation"]
    assert rig.pipeline.scenario_runs == [("db_degradation", started)]
    with pytest.raises(UnknownScenarioError):
        rig.pipeline.simulate("meteor_strike")


def test_simulate_needs_the_generator() -> None:
    with pytest.raises(SimulationDisabledError):
        LineRig().pipeline.simulate("db_degradation")


def test_detection_latency_is_measured_from_the_scenario_start(tmp_path: Path) -> None:
    rig = LineRig(*all_services_feeds())
    rig.pipeline.generator = LogGenerator(rig.clock, tmp_path / "gen.log", seed=1)
    rig.run(WARM)
    started = rig.pipeline.simulate("db_degradation")  # the feeds below stand in for its traffic
    start_db_outage(rig)
    rig.run(30)
    rig.pipeline.simulate("recover")
    end_outage(rig)
    rig.run(5)
    rig.pipeline.simulate("noisy_blip")  # opens nothing: must not appear
    rig.run(20)
    latency = rig.pipeline.stats().detection_latency_s
    assert set(latency) == {"db_degradation"}
    first_open = min(g.opened_at for g in rig.pipeline.manager.group_summaries())
    assert latency["db_degradation"] == pytest.approx(first_open - started, abs=0.1)
    assert 0 < latency["db_degradation"] < 20


def test_sensitivity_changes_what_the_detectors_do() -> None:
    rig = LineRig(*all_services_feeds())
    rig.run(WARM)
    rig.pipeline.set_sensitivity(5.0)
    assert rig.settings.OPEN_Z == 5.0
    feed_of(rig, "claims-service").p_error = 0.10  # a moderate rise
    rig.run(60)
    strict = len(rig.pipeline.manager.group_summaries())
    rig.pipeline.set_sensitivity(2.0)
    rig.run(20)
    assert len(rig.pipeline.manager.group_summaries()) >= strict


def test_health_reports_the_mode() -> None:
    rig = LineRig(AWS_ENABLED=False, LLM_PROVIDER="none")
    rig.run(3)
    health = rig.pipeline.health()
    assert health["status"] == "ok" and health["aws"] == "dryrun" and health["llm"] == "none"
    assert health["uptime_s"] == pytest.approx(3.0)


def test_from_settings_builds_the_generator_only_when_enabled(tmp_path: Path) -> None:
    common = {"LOG_PATH": str(tmp_path / "a.log"), "DB_PATH": str(tmp_path / "s.db")}
    on = Pipeline.from_settings(make_settings(GENERATOR_ENABLED=True, **common))
    off = Pipeline.from_settings(make_settings(GENERATOR_ENABLED=False, **common))
    assert on.generator is not None and off.generator is None
    assert (tmp_path / "s.db").exists()
    assert on.collector is not None and str(on.collector._path) == common["LOG_PATH"]
    for pipeline in (on, off):
        assert pipeline.store is not None
        pipeline.store.close()


# ---- the hub ----------------------------------------------------------------------------


async def test_hub_fans_out_to_every_subscriber_and_stops_after_unsubscribe() -> None:
    hub = Hub()
    a, b = hub.subscribe(), hub.subscribe()
    hub.publish({"type": "x", "data": 1})
    assert (await a.get())["data"] == 1 and (await b.get())["data"] == 1
    hub.unsubscribe(a)
    hub.publish({"type": "x", "data": 2})
    assert a.empty() and (await b.get())["data"] == 2
    assert hub.subscriber_count == 1


async def test_a_slow_subscriber_loses_its_oldest_messages_and_never_blocks() -> None:
    hub = Hub(queue_size=3)
    slow = hub.subscribe()
    for i in range(10):
        hub.publish({"type": "x", "data": i})  # returns immediately every time
    got = [(await asyncio.wait_for(slow.get(), 1))["data"] for _ in range(3)]
    assert got == [7, 8, 9]


def test_publishing_with_no_subscribers_is_fine() -> None:
    Hub().publish({"type": "x", "data": 0})


def test_severity_names_in_messages_match_the_enum() -> None:
    assert {s.name for s in Severity} == {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
