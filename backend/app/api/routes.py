"""REST routes (CLAUDE.md §7). Handlers are `async` on purpose: they then run on the event loop,
the same thread as the pipeline tick, so they never see a half-updated incident."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.models import GroupCard, GroupSummary, Level, LogLine, ServiceStatus, Stats
from app.pipeline import Pipeline, SimulationDisabledError
from generator.scenarios import UnknownScenarioError

router = APIRouter(prefix="/api")

_LEVEL_NAMES: dict[str, Level] = {
    **{level.value: level for level in Level},
    "WARNING": Level.WARN,
    "FATAL": Level.CRITICAL,
}


def pipeline_of(request: Request) -> Pipeline:
    pipeline: Pipeline = request.app.state.pipeline
    return pipeline


class SensitivityRequest(BaseModel):
    open_z: float = Field(ge=2, le=5, description="Robust z needed to open an alert (2 to 5)")


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    return pipeline_of(request).health()


@router.get("/services", response_model=list[ServiceStatus])
async def services(request: Request) -> list[ServiceStatus]:
    return pipeline_of(request).services_view()


@router.get("/incidents", response_model=list[GroupSummary])
async def incidents(
    request: Request,
    status: Annotated[Literal["open", "resolved", "all"], Query()] = "all",
) -> list[GroupSummary]:
    """One card per outage (correlation group), highest priority first."""
    return pipeline_of(request).manager.group_summaries(status)


@router.get("/incidents/{incident_id}", response_model=GroupCard)
async def incident(request: Request, incident_id: str) -> GroupCard:
    card = pipeline_of(request).manager.group_card(incident_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"no incident {incident_id!r}")
    return card


@router.post("/simulate/{scenario}")
async def simulate(request: Request, scenario: str) -> dict[str, Any]:
    try:
        started = pipeline_of(request).simulate(scenario)
    except UnknownScenarioError:
        raise HTTPException(status_code=404, detail=f"unknown scenario {scenario!r}") from None
    except SimulationDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"ok": True, "scenario": scenario, "started_at": started}


@router.post("/aws/test")
async def aws_test(request: Request) -> dict[str, Any]:
    """Send one clearly-labeled test message to CloudWatch and SNS; report each result."""
    return await asyncio.to_thread(pipeline_of(request).notifier.send_test)


@router.post("/config/sensitivity")
async def sensitivity(request: Request, body: SensitivityRequest) -> dict[str, Any]:
    pipeline_of(request).set_sensitivity(body.open_z)
    return {"ok": True, "open_z": body.open_z}


@router.get("/stats", response_model=Stats)
async def stats(request: Request) -> Stats:
    return pipeline_of(request).stats()


@router.get("/logs/recent", response_model=list[LogLine])
async def recent_logs(
    request: Request,
    service: str | None = None,
    level: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[LogLine]:
    """The live log tail (redacted), newest last. ``level`` is one level or a comma list."""
    levels = None
    if level:
        try:
            names = [name.strip().upper() for name in level.split(",") if name.strip()]
            levels = {_LEVEL_NAMES[name] for name in names}
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=f"unknown level {exc.args[0]!r}") from None
    return pipeline_of(request).logfeed.recent(service or None, levels or None, limit)


@router.get("/metrics/history")
async def metrics_history(
    request: Request, seconds: Annotated[float, Query(gt=0, le=3600)] = 300
) -> list[dict[str, Any]]:
    return pipeline_of(request).metrics_history(seconds)
