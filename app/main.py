"""Resource Monitor: FastAPI app.

Serves system metrics, Docker container stats, a live WebSocket
snapshot feed, and the static dashboard frontend.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from . import docker_stats, system_metrics

app = FastAPI(title="Resource Monitor", version="0.1.0")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Never let browsers cache the dashboard assets.

    A tab that stays open keeps running whatever app.js it first loaded;
    stale caches of the dashboard JS have caused "blank" views before.
    """
    response = await call_next(request)
    if not request.url.path.startswith(("/api", "/ws")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

PUSH_INTERVAL = 1.5  # seconds between websocket pushes
CONTAINER_REFRESH_EVERY = 4  # container stats are slower; refresh every Nth push

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/api/system")
def api_system():
    """Host CPU (overall + per core), memory, load averages, uptime."""
    return system_metrics.get_system()


@app.get("/api/containers")
def api_containers():
    """Docker container list with state, status, and live stats."""
    data = docker_stats.get_containers()
    if "error" in data:
        return JSONResponse(data, status_code=503)
    return data


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """Push a full snapshot; container stats every CONTAINER_REFRESH_EVERY push."""
    await websocket.accept()
    tick = 0
    try:
        while True:
            system = await asyncio.to_thread(system_metrics.get_system)
            payload: dict = {"system": system}
            if tick % CONTAINER_REFRESH_EVERY == 0:
                containers = await asyncio.to_thread(docker_stats.get_containers)
                payload["containers"] = containers
            await websocket.send_text(json.dumps(payload))
            tick += 1
            await asyncio.sleep(PUSH_INTERVAL)
    except WebSocketDisconnect:
        pass


# Registered last so /api/* and /ws routes take precedence.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
