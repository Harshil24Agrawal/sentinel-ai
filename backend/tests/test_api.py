"""REST and WebSocket contract (CLAUDE.md §7), against the real FastAPI app."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import LineRig, all_services_feeds, make_settings
from test_pipeline import WARM, end_outage, start_db_outage

from app.main import create_app
from generator.generator import LogGenerator

SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
GROUP_SUMMARY_KEYS = {
    "id", "state", "current_severity", "max_severity", "opened_at", "resolved_at", "title",
    "origin", "affected_services", "has_new_pattern", "notified", "incidents",
}  # fmt: skip
MEMBER_KEYS = {
    "id", "service", "state", "current_severity", "max_severity", "opened_at", "resolved_at",
    "duration_s", "what", "failed_ops", "peak_z", "has_new_pattern", "signals", "escalations",
    "timeline", "ml_score",
}  # fmt: skip
CARD_EXTRA_KEYS = {
    "what", "duration_s", "failed_ops", "peak_z", "hint", "patterns", "samples", "story",
    "blast_radius", "explanation", "what_if", "ml_score",
}  # fmt: skip


def client_for(rig: LineRig) -> TestClient:
    app = create_app(rig.settings, pipeline=rig.pipeline, start_tasks=False)
    return TestClient(app)


@pytest.fixture(scope="module")
def healthy() -> TestClient:
    rig = LineRig(*all_services_feeds(), store=False)
    rig.run(WARM)
    return client_for(rig)


@pytest.fixture(scope="module")
def outage() -> tuple[TestClient, LineRig]:
    """A database outage that has spread to appointment-service and is still open."""
    rig = LineRig(*all_services_feeds(), store=False)
    rig.run(WARM)
    start_db_outage(rig)
    rig.run(40)
    return client_for(rig), rig


# ---- health and services ----------------------------------------------------------------


def test_health(healthy: TestClient) -> None:
    data = healthy.get("/api/health").json()
    assert set(data) == {"status", "uptime_s", "aws", "aws_checks", "llm"}
    assert data["status"] == "ok" and data["aws"] == "dryrun" and data["llm"] == "none"
    assert data["uptime_s"] == pytest.approx(WARM, abs=1)
    assert set(data["aws_checks"]) == {"credentials", "cloudwatch", "sns"}


def test_services_have_the_contract_fields(healthy: TestClient) -> None:
    rows = healthy.get("/api/services").json()
    assert len(rows) == 7
    row = next(r for r in rows if r["name"] == "appointment-service")
    assert set(row) == {
        "name", "status", "volume", "error_rate", "p95_latency", "baseline", "depends_on",
    }  # fmt: skip
    assert set(row["baseline"]) == {"error_rate_median", "error_rate_band", "p95_median", "ready"}
    assert len(row["baseline"]["error_rate_band"]) == 2
    assert row["depends_on"] == ["database", "notification-service"]
    assert row["status"] == "healthy" and row["baseline"]["ready"] is True
    assert {r["status"] for r in rows} == {"healthy"}


def test_services_show_the_outage(outage: tuple[TestClient, LineRig]) -> None:
    client, _ = outage
    status = {r["name"]: r["status"] for r in client.get("/api/services").json()}
    assert status["database"] in SEVERITIES and status["appointment-service"] in SEVERITIES
    assert status["auth-service"] == "healthy"


# ---- incidents: one card per outage -----------------------------------------------------


def test_no_incidents_when_healthy(healthy: TestClient) -> None:
    assert healthy.get("/api/incidents").json() == []
    assert healthy.get("/api/incidents?status=open").json() == []


def test_incidents_returns_one_group_card_with_members_nested(
    outage: tuple[TestClient, LineRig],
) -> None:
    client, _ = outage
    groups = client.get("/api/incidents").json()
    assert len(groups) == 1  # one outage, one card, however many services
    group = groups[0]
    assert set(group) == GROUP_SUMMARY_KEYS
    assert group["state"] == "OPEN" and group["origin"] == "database"
    assert group["current_severity"] in SEVERITIES and group["max_severity"] in SEVERITIES
    assert group["affected_services"][:2] == ["database", "appointment-service"]
    members = group["incidents"]
    assert {m["service"] for m in members} == {"database", "appointment-service"}
    for member in members:
        assert set(member) == MEMBER_KEYS
        assert member["current_severity"] in SEVERITIES  # names, never numbers
        assert member["timeline"] and set(member["timeline"][0]) == {
            "t", "service", "event", "severity", "detail",
        }  # fmt: skip
        assert member["timeline"][-1]["event"] in {"opened", "escalated", "de-escalated"}
    assert group["title"].startswith("database:")


def test_incidents_status_filter(outage: tuple[TestClient, LineRig]) -> None:
    client, _ = outage
    assert len(client.get("/api/incidents?status=open").json()) == 1
    assert client.get("/api/incidents?status=resolved").json() == []
    assert len(client.get("/api/incidents?status=all").json()) == 1
    assert client.get("/api/incidents?status=bogus").status_code == 422


def test_incident_card_has_the_evidence_and_the_explanation(
    outage: tuple[TestClient, LineRig],
) -> None:
    client, _ = outage
    group_id = client.get("/api/incidents").json()[0]["id"]
    card = client.get(f"/api/incidents/{group_id}").json()
    assert set(card) == GROUP_SUMMARY_KEYS | CARD_EXTRA_KEYS
    assert card["id"] == group_id and card["origin"] == "database"
    assert card["hint"].startswith("Database or downstream dependency")
    templates = {p["template"] for p in card["patterns"]}
    assert "Connection pool exhausted after <NUM> ms" in templates
    assert len(card["patterns"]) <= 3 and len(card["samples"]) <= 3
    top = card["patterns"][0]
    assert set(top) == {
        "pattern_id", "template", "count", "expected_per_min", "is_new", "surge_ratio", "service",
    }  # fmt: skip
    assert [e["t"] for e in card["story"]] == sorted(e["t"] for e in card["story"])
    assert {e["service"] for e in card["story"]} == {"database", "appointment-service"}
    radius = {n["service"]: n["status"] for n in card["blast_radius"]["nodes"]}
    assert card["blast_radius"]["origin"] == "database"
    assert radius["database"] == radius["appointment-service"] == "impacted"
    assert radius["patient-portal"] == "at_risk" and radius["auth-service"] == "at_risk"

    explanation = card["explanation"]
    assert set(explanation) == {
        "is_incident", "summary", "probable_cause", "affected_services", "recommended_checks",
        "confidence", "source", "label",
    }  # fmt: skip
    assert explanation["source"] == "rule_based" and "Rule-based" in explanation["label"]
    assert explanation["confidence"] in {"low", "medium", "high"}
    assert explanation["probable_cause"].startswith("Probable cause (not confirmed)")
    assert "root cause" not in " ".join(str(v) for v in explanation.values()).lower()
    assert card["what_if"] is None and card["ml_score"] is None  # stretch


def test_incident_card_by_member_incident_id(outage: tuple[TestClient, LineRig]) -> None:
    client, _ = outage
    group = client.get("/api/incidents").json()[0]
    member_id = group["incidents"][1]["id"]
    assert client.get(f"/api/incidents/{member_id}").json()["id"] == group["id"]


def test_unknown_incident_is_a_404(outage: tuple[TestClient, LineRig]) -> None:
    client, _ = outage
    response = client.get("/api/incidents/grp-nope")
    assert response.status_code == 404 and "grp-nope" in response.json()["detail"]


def test_a_resolved_outage_moves_to_the_resolved_list() -> None:
    rig = LineRig(*all_services_feeds(), store=False)
    rig.run(WARM)
    start_db_outage(rig)
    rig.run(40)
    end_outage(rig)
    for _ in range(260):
        rig.tick()
        if not rig.pipeline.manager.open_services():
            break
    client = client_for(rig)
    assert client.get("/api/incidents?status=open").json() == []
    (group,) = client.get("/api/incidents?status=resolved").json()
    assert group["state"] == "RESOLVED" and group["resolved_at"] is not None
    assert group["notified"] is True
    card = client.get(f"/api/incidents/{group['id']}").json()
    assert "has resolved" in card["explanation"]["summary"]


# ---- controls ---------------------------------------------------------------------------


def test_simulate_starts_a_scenario_in_the_generator(tmp_path: Path) -> None:
    rig = LineRig(*all_services_feeds(lines=2), store=False)
    rig.pipeline.generator = LogGenerator(rig.clock, tmp_path / "gen.log", seed=1)
    rig.run(5)
    client = client_for(rig)
    response = client.post("/api/simulate/db_degradation")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "scenario": "db_degradation", "started_at": rig.clock.now(),
    }  # fmt: skip
    assert rig.pipeline.generator.active_scenarios() == ["db_degradation"]
    assert client.post("/api/simulate/recover").json()["ok"] is True
    assert rig.pipeline.generator.active_scenarios() == []


def test_simulate_unknown_scenario_is_a_404(tmp_path: Path) -> None:
    rig = LineRig(store=False)
    rig.pipeline.generator = LogGenerator(rig.clock, tmp_path / "gen.log")
    assert client_for(rig).post("/api/simulate/meteor_strike").status_code == 404


def test_simulate_without_the_generator_is_a_409() -> None:
    response = client_for(LineRig(store=False)).post("/api/simulate/db_degradation")
    assert response.status_code == 409 and "disabled" in response.json()["detail"]


@pytest.mark.parametrize("value", [2, 3.5, 5])
def test_sensitivity_accepts_the_documented_range(value: float) -> None:
    rig = LineRig(store=False)
    response = client_for(rig).post("/api/config/sensitivity", json={"open_z": value})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "open_z": value}
    assert rig.settings.OPEN_Z == value  # the detectors read it on the next tick


@pytest.mark.parametrize("body", [{"open_z": 1.9}, {"open_z": 5.1}, {"open_z": "high"}, {}])
def test_sensitivity_rejects_everything_else(body: dict) -> None:
    rig = LineRig(store=False)
    assert client_for(rig).post("/api/config/sensitivity", json=body).status_code == 422
    assert rig.settings.OPEN_Z == 3.0


# ---- stats and history ------------------------------------------------------------------


def test_stats_have_the_contract_fields(outage: tuple[TestClient, LineRig]) -> None:
    client, rig = outage
    stats = client.get("/api/stats").json()
    assert set(stats) == {
        "naive_alerts", "sentinel_incidents", "sentinel_notifications", "detection_latency_s",
        "phi_redacted", "delivery_log",
    }  # fmt: skip
    assert stats["sentinel_incidents"] == 1  # groups, not per-service incidents
    assert stats["naive_alerts"] > stats["sentinel_notifications"]
    assert stats["sentinel_notifications"] == rig.pipeline.manager.notifications_emitted
    assert isinstance(stats["detection_latency_s"], dict)


def test_metrics_history(healthy: TestClient) -> None:
    frames = healthy.get("/api/metrics/history").json()
    assert len(frames) == WARM and set(frames[0]) == {"t", "services"}
    assert [f["t"] for f in frames] == sorted(f["t"] for f in frames)
    recent = healthy.get("/api/metrics/history?seconds=10").json()
    assert len(recent) == 10 and recent[-1] == frames[-1]
    assert healthy.get("/api/metrics/history?seconds=0").status_code == 422


def test_cors_allows_the_vite_dev_server_only(healthy: TestClient) -> None:
    ok = healthy.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    other = healthy.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in other.headers


# ---- the running app: lifespan and WebSocket --------------------------------------------


def live_settings(tmp_path: Path, **overrides: object):  # type: ignore[no-untyped-def]
    values: dict[str, object] = {
        "TICK_SECONDS": 0.05,
        "LOG_PATH": str(tmp_path / "app.log"),
        "DB_PATH": str(tmp_path / "sentinel.db"),
        "DRYRUN_PATH": str(tmp_path / "notifications.jsonl"),
        "GENERATOR_ENABLED": False,
    }
    return make_settings(**{**values, **overrides})


def receive_of_type(ws, kind: str) -> dict:  # type: ignore[no-untyped-def]
    """The next WebSocket message of ``kind`` (metrics and logs both arrive every tick)."""
    while True:
        message = ws.receive_json()
        if message["type"] == kind:
            return message


def test_websocket_streams_metrics_and_logs_every_tick(tmp_path: Path) -> None:
    with TestClient(create_app(live_settings(tmp_path))) as client:
        with client.websocket_connect("/ws") as ws:
            received = [ws.receive_json() for _ in range(8)]
    assert {m["type"] for m in received} == {"metrics", "logs"}
    logs = [m["data"] for m in received if m["type"] == "logs"]
    assert logs and all(set(d) == {"lines", "sampled_out", "capped"} for d in logs)
    messages = [m for m in received if m["type"] == "metrics"][:4]
    assert len(messages) == 4
    times = [m["data"]["t"] for m in messages]
    assert times == sorted(times) and len(set(times)) == 4
    services = messages[-1]["data"]["services"]
    assert len(services) == 7
    assert set(services["database"]) == {"volume", "error_rate", "p95_latency", "band", "status"}
    assert services["database"]["status"] == "warming_up"


def test_websocket_subscribers_are_cleaned_up(tmp_path: Path) -> None:
    with TestClient(create_app(live_settings(tmp_path))) as client:
        hub = client.app.state.pipeline.hub  # type: ignore[attr-defined]
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            assert hub.subscriber_count == 1
        for _ in range(100):  # the server notices the closed socket on its next send
            client.get("/api/health")
            if hub.subscriber_count == 0:
                break
        assert hub.subscriber_count == 0


def test_lifespan_starts_and_stops_the_tasks(tmp_path: Path) -> None:
    app = create_app(live_settings(tmp_path, GENERATOR_ENABLED=True))
    with TestClient(app) as client:
        pipeline = client.app.state.pipeline  # type: ignore[attr-defined]
        names = {t.get_name() for t in pipeline._tasks}
        assert names == {"collector", "pipeline", "generator"}
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/simulate/noisy_blip").status_code == 200  # generator is wired
    assert all(t.done() for t in pipeline._tasks) or pipeline._tasks == []
    assert (tmp_path / "sentinel.db").exists()


def test_lifespan_without_a_generator_keeps_simulate_disabled(tmp_path: Path) -> None:
    with TestClient(create_app(live_settings(tmp_path))) as client:
        assert client.post("/api/simulate/db_degradation").status_code == 409
        pipeline = client.app.state.pipeline  # type: ignore[attr-defined]
        assert {t.get_name() for t in pipeline._tasks} == {"collector", "pipeline"}


def test_a_running_pipeline_reads_lines_from_the_log_file(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    log = Path(settings.LOG_PATH)
    log.write_text("")
    with TestClient(create_app(settings)) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # the collector is now tailing the file
            with open(log, "a", encoding="utf-8") as f:
                for i in range(20):
                    f.write(f"2026-09-28T10:00:01.000Z INFO database latency_ms=9 Query {i} ok\n")
            volume = 0
            for _ in range(60):
                volume = receive_of_type(ws, "metrics")["data"]["services"]["database"]["volume"]
                if volume >= 20:
                    break
            assert volume >= 20
            # the same lines reach the live feed (sampled: at most 10 INFO lines per tick)
            recent = client.get("/api/logs/recent?service=database").json()
            assert len(recent) == 20 and recent[-1]["message"] == "Query 19 ok"


# ---- the dashboard is served from the same origin ---------------------------------------


def test_frontend_index_is_served_at_the_root(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        "<html><title>SentinelAI dashboard</title></html>", encoding="utf-8"
    )
    rig = LineRig(store=False, FRONTEND_DIR=str(tmp_path))
    client = client_for(rig)
    page = client.get("/")
    assert page.status_code == 200 and "SentinelAI dashboard" in page.text
    assert page.headers["content-type"].startswith("text/html")
    assert client.get("/api/health").json()["status"] == "ok"  # the API still wins
    assert client.get("/no-such-file.js").status_code == 404


def test_root_is_a_404_when_there_is_no_frontend(tmp_path: Path) -> None:
    assert client_for(LineRig(store=False, FRONTEND_DIR=str(tmp_path))).get("/").status_code == 404


def test_the_repos_own_frontend_is_served_by_default() -> None:
    from app.main import DEFAULT_FRONTEND_DIR

    if not (DEFAULT_FRONTEND_DIR / "index.html").is_file():
        pytest.skip("no frontend/index.html in this checkout")
    client = client_for(LineRig(store=False))
    page = client.get("/")
    assert page.status_code == 200 and page.text == (DEFAULT_FRONTEND_DIR / "index.html").read_text(
        encoding="utf-8"
    )
    assert client.get("/api/services").status_code == 200
