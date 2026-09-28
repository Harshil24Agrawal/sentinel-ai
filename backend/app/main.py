"""FastAPI app: the lifespan builds the pipeline and starts (and later stops) its tasks."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import routes, ws
from app.config import Settings, get_settings
from app.pipeline import Pipeline

DEFAULT_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
_STANDARD_RECORD_KEYS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Extra fields are passed through; raw log lines never are (the
    pipeline logs stage names and exception types only)."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD_RECORD_KEYS})
        if record.exc_info:
            entry["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def create_app(
    settings: Settings | None = None,
    *,
    pipeline: Pipeline | None = None,
    start_tasks: bool = True,
) -> FastAPI:
    """Build the app. Tests pass their own ``pipeline`` (and ``start_tasks=False`` to drive its
    ticks by hand on a FakeClock)."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.LOG_LEVEL)
        if getattr(app.state, "pipeline", None) is None:
            app.state.pipeline = Pipeline.from_settings(settings)
        if start_tasks:
            app.state.pipeline.start()
        try:
            yield
        finally:
            if start_tasks:
                await app.state.pipeline.stop()

    app = FastAPI(title="SentinelAI", lifespan=lifespan)
    app.state.pipeline = pipeline
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(routes.router)
    app.include_router(ws.router)
    # Last, so /api/* and /ws win: the dashboard is served from the same origin at /
    frontend = Path(settings.FRONTEND_DIR) if settings.FRONTEND_DIR else DEFAULT_FRONTEND_DIR
    if (frontend / "index.html").is_file():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app


app = create_app()  # `uvicorn app.main:app`; nothing starts until the server's lifespan runs
