# Containerized Resource Monitor — Plan

## Architecture overview

A compose stack of two Docker containers on a private compose network:

- **monitor** — a container running a **FastAPI** (Python) backend + a **static single-page frontend**, exposing one HTTP port (`1010`).
- **socket-proxy** — a digest-pinned `lscr.io/linuxserver/socket-proxy` sidecar that fronts the host Docker socket with a read-only, endpoint-restricted view. It is never exposed to the outside.

The monitor gets host data two ways:

1. **Host CPU/RAM** — read from `/proc/stat` and `/proc/meminfo`. These files are host-wide (not PID-namespace scoped), so a plain container can read host stats with **no privileged mode and no extra mounts**. `psutil` wraps this.
2. **Docker containers** — query the Docker Engine API via the `docker` Python SDK through the **read-only socket proxy** (`DOCKER_HOST=tcp://socket-proxy:2375`). The proxy mounts `/var/run/docker.sock` (ro) and allows only `GET /_ping`, `/version`, `/containers/*`, `/images/*` (`POST=0`); the monitor container never sees the socket itself.

## Requirements coverage

| Requirement | How it's met |
|---|---|
| Runs inside Docker | `docker-compose.yml` stack: hardened `monitor` container (built from `Dockerfile`) + `socket-proxy` sidecar |
| Monitor system RAM usage | `/proc/meminfo` via `psutil` (total/used/free/available/swap) |
| Monitor CPU usage across threads | Per-core/per-hardware-thread usage from `/proc/stat` deltas, plus overall % and load averages |
| View Docker containers + status | Docker Engine API via read-only socket proxy (no mutation possible) |
| Visual web dashboard | Static SPA (plain JS + Chart.js) served by FastAPI, live updates via WebSocket |

## Components

### 1. Backend (Python 3.12, FastAPI + Uvicorn)
- `GET /api/system`
  - **RAM**: total / used / free / available / cached / buffers / swap + usage % (from `/proc/meminfo`)
  - **CPU per thread/core**: per-core usage % for `cpu0..cpuN` (sampled from `/proc/stat` deltas — each hardware core/thread), plus overall %, load averages, uptime
- `GET /api/containers` — name, image, state (running/exited/paused/…), status, health, uptime, per-container CPU % and memory used/limit
  - Per-container CPU %: one **non-streaming** `stats` call per container — the daemon samples for ~1 s internally and returns both `precpu_stats` and `cpu_stats`, so a single call yields a real short-window %. Calls run **concurrently** across containers (`ThreadPoolExecutor`, max 8 workers), so a pass takes ~1 s total regardless of container count (capped at 25 sampled).
- `WS /ws` — pushes a full system snapshot every ~1.5 s so the dashboard is live without hammering the API; container stats ride along every 4th push (~6 s) since they're the slower path
- **Optional auth** — `MONITOR_TOKEN` env var (off by default); when set, every HTTP route (API + static) and the WebSocket handshake require the token via `Authorization: Bearer` or `?token=` (constant-time comparison); the WS denial happens at the handshake itself (no data ever flows)
- Graceful degradation: if the proxy/daemon is unreachable, system metrics still work and the containers API returns a clear 503 error

### 2. Frontend (static SPA, no build server needed)
- Plain JS + **Chart.js** (vendored) served from the FastAPI app (keeps the image single-stage and dependency-light)
- Dashboard panels:
  - **CPU / Memory cards**: overall utilization with numbers + live sparklines
  - **Timeline**: system CPU % and RAM % over time with 5 min / 15 min / 1 hour windows, plus a per-container CPU % chart; client-side history capped at 2400 points (~1 h at 1.5 s pushes)
  - **Per-core CPU**: live chart, one series per core/hardware thread, color-coded green/yellow/red
  - **RAM breakdown**: stacked used/cached/buffers/free bar plus swap totals
  - **Containers table**: name, image, state badge, health, uptime, live CPU % and memory used/limit
- Consumes the WebSocket (push every ~1.5 s); falls back to polling `/api/system` + `/api/containers` every 3 s if the socket drops
- Static assets are served `no-cache` so a long-open tab can't run stale frontend code (this caused a "blank" view before); `Referrer-Policy: no-referrer` keeps any `?token=` out of Referer headers
- When auth is enabled the page is opened as `?token=<secret>` and that value is attached to every fetch and the WebSocket URL (JS can't set headers on WS handshakes)

### 3. Docker packaging (hardened)
- **Dockerfile**: `python:3.12-slim` **pinned by digest** → install pinned `fastapi uvicorn[standard] psutil docker` → `COPY` app + static frontend → `USER monitor` (non-root) → uvicorn on `0.0.0.0:1010`
- **docker-compose.yml** — two services:
  ```yaml
  services:
    monitor:
      build: .
      ports: ["1010:1010"]
      environment: [DOCKER_HOST=tcp://socket-proxy:2375]
      read_only: true
      tmpfs: ["/tmp"]
      cap_drop: [ALL]
      security_opt: ["no-new-privileges:true"]
      restart: unless-stopped
    socket-proxy:   # lscr.io/linuxserver/socket-proxy, digest-pinned
      environment: [PING=1, VERSION=1, CONTAINERS=1, IMAGES=1, POST=0, ...all else 0]
      volumes: ["/var/run/docker.sock:/var/run/docker.sock:ro"]
      read_only: true
      tmpfs: ["/run"]
      cap_drop: [ALL]
      security_opt: ["no-new-privileges:true"]
      restart: unless-stopped
  ```
- Runs as **non-root**; the docker socket is only mounted (ro) into the proxy, which enforces a read-only, endpoint-restricted view of the daemon. Both containers run `read_only` with all capabilities dropped. Optional `MONITOR_TOKEN` bearer auth for all requests.
- Verified end-to-end: mutation endpoints (`POST/DELETE /containers*`, `/build`, `/exec`, …) return 403 through the proxy while the reads the app needs return 200.

### 4. README
- Quickstart (`docker compose up --build`) noting the repo ships the compose file + `Dockerfile` and the image is built from source; what each panel shows; **Security** section (read-only proxy, non-root, read-only FS, dropped caps, digest pinning, optional token auth, port published on all interfaces); troubleshooting (proxy/503, 401, host CPU visibility)

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
