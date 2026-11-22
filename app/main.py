"""Resource Monitor: FastAPI app.

Serves system metrics, Docker container stats, a live WebSocket
snapshot feed, and the static dashboard frontend.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response

from . import docker_stats, system_metrics

app = FastAPI(title="Resource Monitor", version="0.1.0")

# Read-only CORS so browser-based dashboards (e.g. the Home Assistant
# custom card) can poll the API cross-origin. The token middleware below
# still gates every non-preflight request when MONITOR_TOKEN is set;
# no write endpoint is exposed by CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Authorization"],
)

# Optional shared-secret auth. Set MONITOR_TOKEN in the environment to
# require a token on every request; the dashboard is then opened as
# http://host:1010/?token=<MONITOR_TOKEN> (the frontend attaches it to
# fetches and the WebSocket URL, where headers can't be set from JS).
MONITOR_TOKEN = os.environ.get("MONITOR_TOKEN", "").strip()


def _authorized(headers, query_params) -> bool:
    """Check the bearer token / ?token= query parameter (constant-time)."""
    if not MONITOR_TOKEN:
        return True
    token = str(query_params.get("token", ""))
    if token and secrets.compare_digest(token, MONITOR_TOKEN):
        return True
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        bearer = auth.split(None, 1)[1].strip()
        if bearer and secrets.compare_digest(bearer, MONITOR_TOKEN):
            return True
    return False


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


@app.middleware("http")
async def require_token(request: Request, call_next):
    """Optional bearer-token auth for all HTTP routes (API + static).

    CORS preflights are let through: browsers send them without credentials,
    and they expose no data (CORS answers them before the route runs).
    """
    if request.method == "OPTIONS":
        return await call_next(request)
    if MONITOR_TOKEN and not _authorized(request.headers, request.query_params):
        return JSONResponse({"error": "unauthorized: missing or bad token"}, status_code=401)
    return await call_next(request)

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
    if MONITOR_TOKEN and not _authorized(websocket.headers, websocket.query_params):
        # Reject the handshake itself with HTTP 401 (no data ever flows).
        # send_denial_response is the starlette >=1 API; it wraps the
        # response so uvicorn emits a real HTTP reply to the upgrade.
        await websocket.send_denial_response(
            Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        )
        return
    await websocket.accept()
    tick = 0
    try:
        while True:
            system = await asyncio.to_thread(system_metrics.get_system)
            payload: dict = {"system": system}
            if tick % CONTAINER_REFRESH_EVERY == 0:
                try:
                    containers = await asyncio.to_thread(docker_stats.get_containers)
                except Exception as exc:
                    # Never kill the live feed over a stats error: report
                    # the failure in the payload and keep pushing.
                    containers = {"error": str(exc)}
                payload["containers"] = containers
            await websocket.send_text(json.dumps(payload))
            tick += 1
            await asyncio.sleep(PUSH_INTERVAL)
    except WebSocketDisconnect:
        pass


# Registered last so /api/* and /ws routes take precedence.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
