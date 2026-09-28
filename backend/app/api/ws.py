"""WebSocket endpoint: every pipeline message, as it happens (CLAUDE.md §7)."""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.pipeline import Pipeline

router = APIRouter()


@router.websocket("/ws")
async def stream(websocket: WebSocket) -> None:
    pipeline: Pipeline = websocket.app.state.pipeline
    await websocket.accept()
    queue = pipeline.hub.subscribe()

    async def send() -> None:
        latest = pipeline.latest_metrics()
        if latest is not None:  # a page that just connected should not wait a whole tick
            await websocket.send_json({"type": "metrics", "data": latest})
        while True:
            await websocket.send_json(await queue.get())

    async def watch_for_disconnect() -> None:
        while True:
            await websocket.receive_text()  # raises WebSocketDisconnect when the client leaves

    sender = asyncio.create_task(send())
    watcher = asyncio.create_task(watch_for_disconnect())
    try:
        await asyncio.wait({sender, watcher}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (sender, watcher):
            task.cancel()
        for task in (sender, watcher):
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await task
        pipeline.hub.unsubscribe(queue)
