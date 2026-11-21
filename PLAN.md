# Containerized Resource Monitor — Plan

## Architecture overview

A single Docker container running a **FastAPI** (Python) backend + a **static single-page frontend**, exposing one HTTP port. It gets host data two ways:

1. **Host CPU/RAM** — read from `/proc/stat` and `/proc/meminfo`. These files are host-wide (not PID-namespace scoped), so a plain container can read host stats with **no privileged mode and no extra mounts**. `psutil` wraps this.
2. **Docker containers** — query the Docker Engine API via the `docker` Python SDK through a **read-only socket proxy** (`lscr.io/linuxserver/socket-proxy`). The proxy mounts `/var/run/docker.sock` (ro) and exposes only `GET /_ping`, `/version`, `/containers/*`, `/images/*` (`POST=0`); the monitor container never sees the socket and connects via `DOCKER_HOST=tcp://socket-proxy:2375`.

## Requirements coverage

| Requirement | How it's met |
|---|---|
| Runs inside Docker | Single container + `docker-compose.yml` |
| Monitor system RAM usage | `/proc/meminfo` via `psutil` (total/used/free/available/swap) |
| Monitor CPU usage across threads | Per-core/per-hardware-thread usage from `/proc/stat` deltas, plus overall % and load averages |
| View Docker containers + status | Docker Engine API via read-only socket proxy (no mutation possible) |
| Visual web dashboard | Static SPA (plain JS + Chart.js) served by FastAPI, live updates via WebSocket |

## Components

### 1. Backend (Python 3.12, FastAPI + Uvicorn)
- `GET /api/system`
  - **RAM**: total / used / free / available / swap + usage % (from `/proc/meminfo`)
  - **CPU per thread/core**: per-core usage % for `cpu0..cpuN` (sampled from `/proc/stat` deltas — each hardware core/thread), plus overall %, load averages, uptime
- `GET /api/containers` — name, image, state (running/exited/paused/…), status, health, uptime, per-container CPU % and memory used/limit (via `docker.containers.list()` + per-container `stats`)
- `WS /ws` — pushes a full snapshot every ~1.5 s so the dashboard is live without hammering the API
- Graceful degradation: if the docker socket is missing/permission-denied, system metrics still work and the API returns a clear error for containers

### 2. Frontend (static SPA, no build server needed)
- Plain JS + **Chart.js** served from the FastAPI app (keeps the image single-stage and dependency-light)
- Dashboard panels:
  - **System header**: overall CPU bar + RAM bar with numbers
  - **Per-core CPU**: live line/bar chart, one series per core/thread
  - **RAM breakdown**: stacked bar (used/free/available/swap)
  - **Containers table**: name, status badge (running/failed/exited…), CPU %, memory, uptime; color-coded states
- Consumes the WebSocket; falls back to polling `/api/system` + `/api/containers` every 2 s

### 3. Docker packaging (hardened)
- **Dockerfile**: `python:3.12-slim` **pinned by digest** → install pinned `fastapi uvicorn[standard] psutil docker` → `COPY` app + static frontend → `USER monitor` (non-root) → uvicorn on `0.0.0.0:8080`
- **docker-compose.yml** — two services:
  ```yaml
  services:
    monitor:
      build: .
      ports: ["8080:8080"]
      environment: [DOCKER_HOST=tcp://socket-proxy:2375]
      read_only: true
      cap_drop: [ALL]
      security_opt: ["no-new-privileges:true"]
      restart: unless-stopped
    socket-proxy:   # lscr.io/linuxserver/socket-proxy, digest-pinned
      environment: [PING=1, VERSION=1, CONTAINERS=1, IMAGES=1, POST=0, ...all else 0]
      volumes: ["/var/run/docker.sock:/var/run/docker.sock:ro"]
      read_only: true
      cap_drop: [ALL]
      security_opt: ["no-new-privileges:true"]
      restart: unless-stopped
  ```
- Runs as **non-root**; the docker socket is only mounted (ro) into the proxy, which enforces a read-only, endpoint-restricted view of the daemon. Optional `MONITOR_TOKEN` bearer auth for all requests.

### 4. README
- Quickstart (`docker compose up --build`), what each panel shows, **Security** section (read-only proxy, non-root, read-only FS, dropped caps, digest pinning, optional token auth), troubleshooting (proxy/503, 401, host CPU visibility)

## Assumptions
1. **"CPU usage across threads"** = per-core/per-hardware-thread utilization (from `/proc/stat`), plus per-container CPU. True per-process-thread sampling isn't visible from inside a container (PID namespace isolation) unless run with `--pid=host`, a security trade-off that could be added later as an optional profile.
2. **Stack**: Python/FastAPI for simplicity; frontend is plain JS + Chart.js (no build step).

## Build order
1. Project scaffold (`Dockerfile`, `docker-compose.yml`, `app/`, `static/`)
2. System metrics API (RAM + per-core CPU)
3. Docker containers API
4. WebSocket push
5. Frontend dashboard
6. `docker compose up` + verify all panels against a live host

## Git
- Remote: `git@github.com:etanyxyz/System-Monitor.git`
- Commits via SSH key
