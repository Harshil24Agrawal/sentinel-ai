"""CLAUDE.md §10 end to end: generator -> FILE -> collector -> pipeline -> API, on a FakeClock,
over ten simulated minutes at realistic traffic. Slow: run with `pytest -m "slow or not slow"`."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from helpers import START, make_settings

from app.clock import FakeClock
from app.main import create_app
from app.models import IncidentState
from app.pipeline import Pipeline
from app.store import Store
from generator.generator import LogGenerator, format_ts
from generator.services import load_services

pytestmark = pytest.mark.slow

# PHI shapes the generator (and the injected lines below) can emit, plus name pairs
PHI = re.compile(
    r"MBR-?\d{6,}|\b\d{3}-\d{2}-\d{4}\b|[\w.+-]+@example\.com"
    r"|\b(?:Jane|John|Maria|Wei|Aisha|Carlos|Priya|Tom) "
    r"(?:Doe|Smith|Garcia|Chen|Khan|Rivera|Patel|Miller)\b"
)
PHI_ERROR_LINE = (
    "{ts} ERROR database latency_ms=3001 Connection pool exhausted after 3001 ms "
    'member_id=MBR1234567 ssn=123-45-6789 email=jane.doe@example.com patient_name="Jane Doe"\n'
)


class Sim:
    """One simulated second per ``step``: the generator writes the FILE, the collector reads it."""

    def __init__(self, tmp_path: Path) -> None:
        self.log = tmp_path / "app.log"
        self.dryrun_path = tmp_path / "notifications_dryrun.jsonl"
        self.clock = FakeClock(START)
        settings = make_settings(
            LOG_PATH=str(self.log), DB_PATH=":memory:", DRYRUN_PATH=str(self.dryrun_path)
        )
        self.generator = LogGenerator(self.clock, self.log, seed=7)
        self.pipeline = Pipeline(
            self.clock,
            settings,
            services=load_services(),
            store=Store(":memory:"),
            generator=self.generator,
        )
        self.inject_phi = False
        self.seconds = 0

    def step(self) -> None:
        self.generator.tick()
        if self.inject_phi:  # PHI inside ERROR lines: they end up in samples and patterns
            with open(self.log, "a", encoding="utf-8") as f:
                for _ in range(3):
                    f.write(PHI_ERROR_LINE.format(ts=format_ts(self.clock.now())))
        self.pipeline.tick_once(self.pipeline.collector.poll_once())
        self.clock.advance(1)
        self.seconds += 1

    def run(self, n: int) -> None:
        for _ in range(n):
            self.step()

    def run_until_settled(self, limit: int = 300) -> None:
        for _ in range(limit):
            self.step()
            if not self.pipeline.manager.open_services():
                return
        raise AssertionError("incidents did not resolve")

    def simulate(self, scenario: str) -> None:
        self.pipeline.simulate(scenario)


@pytest.fixture(scope="module")
def sim(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    s = Sim(tmp_path_factory.mktemp("e2e"))
    s.run(180)  # healthy
    healthy_groups = s.pipeline.manager.groups_opened

    s.simulate("db_degradation")
    s.inject_phi = True
    s.run(75)
    mid_outage_samples = s.pipeline.aggregator.samples("database", 20)
    s.simulate("recover")
    s.inject_phi = False
    s.run_until_settled()
    after_db = s.pipeline.manager.groups_opened

    s.simulate("noisy_blip")
    s.run(70)
    after_blip = s.pipeline.manager.groups_opened

    s.simulate("silent_service")
    s.run(60)
    s.simulate("recover")
    s.run_until_settled()
    if s.seconds < 600:  # "ten simulated minutes"
        s.run(600 - s.seconds)

    app = create_app(s.pipeline.settings, pipeline=s.pipeline, start_tasks=False)
    return SimpleNamespace(
        s=s,
        client=TestClient(app),
        healthy_groups=healthy_groups,
        mid_outage_samples=mid_outage_samples,
        after_db=after_db,
        after_blip=after_blip,
    )


def test_ten_simulated_minutes_ran_through_the_file_and_collector(sim: SimpleNamespace) -> None:
    assert sim.s.seconds >= 600
    assert sim.s.log.stat().st_size > 5_000_000  # a genuinely large, growing log file
    assert sim.s.pipeline.errors == {}  # no stage ever failed
    assert sim.s.pipeline.parser.unparsed == 0 and sim.s.pipeline.aggregator.unparsed == 0
    assert sim.s.pipeline.queue.empty()


def test_a_healthy_system_stays_quiet(sim: SimpleNamespace) -> None:
    assert sim.healthy_groups == 0


def test_each_fault_became_exactly_one_incident_and_the_blip_none(sim: SimpleNamespace) -> None:
    assert sim.after_db == 1  # db_degradation: one outage, however many services it touched
    assert sim.after_blip == 1  # noisy_blip opened nothing
    assert sim.s.pipeline.manager.groups_opened == 2  # ...and silent_service made the second
    groups = sim.client.get("/api/incidents").json()
    assert {g["origin"] for g in groups} == {"database", "notification-service"}
    assert {g["state"] for g in groups} == {"RESOLVED"}  # everything recovered and resolved
    db = next(g for g in groups if g["origin"] == "database")
    assert len(db["incidents"]) >= 3  # database, appointment, claims (and usually notification)


def test_stats_naive_alerts_dwarf_sentinel_notifications(sim: SimpleNamespace) -> None:
    stats = sim.client.get("/api/stats").json()
    assert stats["naive_alerts"] > stats["sentinel_notifications"] >= 2
    assert stats["naive_alerts"] > 10 * stats["sentinel_incidents"]
    assert stats["sentinel_incidents"] == 2
    latency = stats["detection_latency_s"]
    assert set(latency) == {"db_degradation", "silent_service"}  # not the blip, not recover
    assert 0 < latency["db_degradation"] < 30 and 0 < latency["silent_service"] < 30
    assert stats["phi_redacted"] > 100


def test_notifications_are_few_and_the_db_card_explains_itself(sim: SimpleNamespace) -> None:
    notes = sim.s.pipeline.notifications
    assert 2 <= len(notes) <= 6  # <= 3 per outage
    groups = sim.client.get("/api/incidents").json()
    db_id = next(g["id"] for g in groups if g["origin"] == "database")
    card = sim.client.get(f"/api/incidents/{db_id}").json()
    assert card["hint"].startswith("Database or downstream dependency")
    assert any(p["is_new"] and "pool exhausted" in p["template"].lower() for p in card["patterns"])
    assert card["explanation"]["source"] == "rule_based"
    notification_card = next(
        c for g in groups if g["origin"] == "notification-service"
        for c in [sim.client.get(f"/api/incidents/{g['id']}").json()]
    )  # fmt: skip
    assert "stopped logging" in notification_card["hint"]


def test_phi_never_leaves_the_process(sim: SimpleNamespace) -> None:
    """The raw log is full of PHI; nothing derived from it may contain any."""
    raw = sim.s.log.read_text(encoding="utf-8")
    assert len(PHI.findall(raw)) > 500  # the scan below means something

    pipeline, client = sim.s.pipeline, sim.client
    store = pipeline.store
    assert store is not None
    groups = client.get("/api/incidents").json()
    cards = [client.get(f"/api/incidents/{g['id']}").json() for g in groups]
    stored = store.list_incidents()

    # Drain and deliver whatever the notifier queued (CloudWatch state-change events and SNS
    # notification payloads, incl. the readable email body) to the local dry-run file, exactly
    # as the background worker would -- so the file on disk is scanned, not just the in-memory
    # `pipeline.notifications` view the dashboard reads.
    queued = []
    while not pipeline.notifier.queue.empty():
        queued.append(pipeline.notifier.queue.get_nowait())
    if queued:
        pipeline.notifier.deliver(queued)
    assert sim.s.dryrun_path.exists()
    dryrun_text = sim.s.dryrun_path.read_text(encoding="utf-8")
    # Prove the scan below actually exercises the new mobile SNS format and the thinned
    # CloudWatch event vocabulary, not just whatever happened to be there before.
    assert '"event": "incident_opened"' in dryrun_text
    assert '"event": "incident_updated"' not in dryrun_text  # per-tick noise is gone
    assert "CHECK FIRST:" in dryrun_text and "WHAT'S WRONG:" in dryrun_text
    assert "WHAT HAPPENED:" in dryrun_text and "Ref: grp-" in dryrun_text
    assert "RESOLVED: " in dryrun_text and "No action needed." in dryrun_text

    outputs = {
        "incident list": json.dumps(groups),
        "incident cards": json.dumps(cards),
        "notifications": json.dumps(pipeline.notifications),
        "notifier dry-run file (CloudWatch events + SNS bodies)": dryrun_text,
        "metrics history": json.dumps(client.get("/api/metrics/history").json()),
        "services": json.dumps(client.get("/api/services").json()),
        "stats": json.dumps(client.get("/api/stats").json()),
        "stored incidents": " ".join(i.model_dump_json() for i in stored),
        "stored events": json.dumps([store.events(i.id) for i in stored]),
        "aggregator samples": json.dumps(sim.mid_outage_samples),
    }
    for name, text in outputs.items():
        assert PHI.findall(text) == [], f"PHI leaked into the {name}"

    # ...and the PHI really did pass through the ERROR lines that fed those samples
    assert any("[REDACTED]" in line for line in sim.mid_outage_samples)
    assert any("member_id=[REDACTED]" in line for line in sim.mid_outage_samples)


def test_every_incident_was_persisted_and_ended_resolved(sim: SimpleNamespace) -> None:
    store = sim.s.pipeline.store
    assert store is not None
    stored = store.list_incidents()
    assert len(stored) == len(sim.s.pipeline.manager.incidents()) >= 4
    assert {i.state for i in stored} == {IncidentState.RESOLVED}
    for incident in stored:
        kinds = [e["kind"] for e in store.events(incident.id)]
        assert kinds[0] == "opened" and kinds[-1] == "resolved"
