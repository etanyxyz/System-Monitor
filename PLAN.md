# Containerized Resource Monitor — Plan

## Architecture overview

A single Docker container on the host:

- **monitor** — a container running a **FastAPI** (Python) backend + a **static single-page frontend**, exposing one HTTP port (`1010`). It includes an **in-process read-only gateway** (`app/docker_gateway.py`) that speaks the Docker Engine API directly over the read-only-mounted host socket — the socket-proxy sidecar has been folded into this container.

The monitor gets host data two ways:

1. **Host CPU/RAM** — read from `/proc/stat` and `/proc/meminfo`. These files are host-wide (not PID-namespace scoped), so a plain container can read host stats with **no privileged mode and no extra mounts**. `psutil` wraps this.
2. **Docker containers** — the Engine API via the in-process gateway. It connects to `/var/run/docker.sock` (mounted `:ro`) with raw HTTP/1.1 over the unix socket (stdlib `socket`, no docker-py) and is hard-coded to issue only `GET /_ping`, `/version`, `/containers/json`, `/containers/{id}/json`, `/containers/{id}/stats`, `/images/{ref}/json` — the same read-only allow-list the socket-proxy sidecar used to enforce. Nothing else can be requested.

## Requirements coverage

| Requirement | How it's met |
|---|---|
| Runs inside Docker | Single hardened `monitor` container (built from `Dockerfile`) via `docker-compose.yml` |
| One container only | The socket-proxy sidecar is replaced by the in-process gateway (`app/docker_gateway.py`); no third-party proxy image, no `DOCKER_HOST` |
| Monitor system RAM usage | `/proc/meminfo` via `psutil` (total/used/free/available/swap) |
| Monitor CPU usage across threads | Per-core/per-hardware-thread usage from `/proc/stat` deltas, plus overall % and load averages |
| View Docker containers + status | Docker Engine API via the in-process read-only gateway (no mutation possible) |
| Visual web dashboard | Static SPA (plain JS + Chart.js) served by FastAPI, live updates via WebSocket |
| No environment variables | The shipped compose file sets none; `MONITOR_TOKEN` (optional bearer auth) is the only supported env var and is off by default |
| Home Assistant sensors | Custom integration (`custom_components/resource_monitor`) installable via HACS/Marketplace: config flow, coordinator, system/per-core/container sensors |
| Aesthetic dashboard card | Separate Lovelace plugin repo (`System-Monitor-Card`): CPU ring, memory bars, container rows, visual editor |

## Components

### 1. Backend (Python 3.12, FastAPI + Uvicorn)
- `GET /api/system`
  - **RAM**: total / used / free / available / cached / buffers / swap + usage % (from `/proc/meminfo`)
  - **CPU per thread/core**: per-core usage % for `cpu0..cpuN` (sampled from `/proc/stat` deltas — each hardware core/thread), plus overall %, load averages, uptime
- `GET /api/containers` — name, image, state (running/exited/paused/…), status, health, uptime, per-container CPU % and memory used/limit
  - Per-container CPU %: one **non-streaming** `stats` call per running container — the daemon samples for ~1 s internally and returns both `precpu_stats` and `cpu_stats`, so a single call yields a real short-window %. Calls run **concurrently** (`ThreadPoolExecutor`, max 8 workers), so a pass takes ~1 s total (capped at 25 sampled containers).
  - `exit_code` for non-running containers comes from one inspect each (`/containers/{id}/json`); running containers need no inspect.
- `WS /ws` — pushes a full system snapshot every ~1.5 s so the dashboard is live without hammering the API; container stats ride along every 4th push (~6 s) since they're the slower path
- **Optional auth** — `MONITOR_TOKEN` env var (off by default, not set in the shipped compose file); when set, every HTTP route (API + static) and the WebSocket handshake require the token via `Authorization: Bearer` or `?token=` (constant-time comparison); the WS denial happens at the handshake itself (no data ever flows)
- Graceful degradation: if the daemon is unreachable, system metrics still work and the containers API returns a clear 503 error; the WS feed keeps pushing and reports the failure in the payload

### 1b. In-process read-only gateway (`app/docker_gateway.py`)
- Raw HTTP/1.1 over the unix socket (stdlib `socket`): request, header parse, chunked/gzip body decode, 10 s timeout
- Hard-coded GET-only allow-list enforced **before** any byte is sent:
  `/_ping`, `/version`, `/containers/json`, `/containers/{id}/json`, `/containers/{id}/stats`, `/images/{ref}/json` (image refs: name[:tag], domain[:port]/name[:tag], @sha256 digest, bare digest)
- Non-2xx daemon responses and connect failures raise `GatewayError`, which `docker_stats` turns into `{"error": …}`
- Replaces docker-py + the socket-proxy sidecar: `requirements.txt` is down to `fastapi`, `uvicorn[standard]`, `psutil`

### 2. Frontend (static SPA, no build server needed)
- Plain JS + **Chart.js** (vendored) served from the FastAPI app (keeps the image single-stage and dependency-light)
- Dashboard panels:
  - **CPU / Memory cards**: overall utilization with numbers + live sparklines
  - **Timeline**: system CPU % and RAM % over time with 5 min / 15 min / 1 hour windows, plus a per-container CPU % chart; client-side history capped at 2400 points (~1 h at 1.5 s pushes)
  - **Per-core CPU**: live chart, one series per core/hardware thread, color-coded green/yellow/red
  - **RAM breakdown**: stacked used/cached/buffers/free bar plus swap totals
  - **Containers table**: all `docker ps` columns — ID, Image, Command, Created (relative time), Status (docker-style, e.g. "Up 3 hours (healthy)"), Ports, Name, Labels — plus live CPU % and memory used/limit
- Consumes the WebSocket (push every ~1.5 s); falls back to polling `/api/system` + `/api/containers` every 3 s if the socket drops
- Static assets are served `no-cache` so a long-open tab never runs stale frontend code; `Referrer-Policy: no-referrer` keeps any `?token=` out of Referer headers
- Container fields come from `GET /containers/json` (the `docker ps` data: Status, Ports, Command, Labels) through the gateway, plus one inspect per *exited* container for the exit code; live CPU/memory from a non-streaming `stats` call per running container (thread-pooled, capped at 25)
- When auth is enabled the page is opened as `?token=<secret>` and that value is attached to every fetch and the WebSocket URL (JS can't set headers on WS handshakes)

### 3. Docker packaging (hardened, single container)
- **Dockerfile**: `python:3.12-slim` **pinned by digest** → install pinned `fastapi uvicorn[standard] psutil` → `COPY` app + static frontend → `USER monitor` (non-root) → uvicorn on `0.0.0.0:1010`
- **docker-compose.yml** — one service, zero environment variables:
  ```yaml
  services:
    monitor:
      build: .
      ports: ["1010:1010"]
      # optional auth, off by default:
      # environment: [MONITOR_TOKEN=change-me]
      volumes: ["/var/run/docker.sock:/var/run/docker.sock:ro"]
      group_add: ["985"]   # host docker GID (getent group docker)
      read_only: true
      tmpfs: ["/tmp"]
      cap_drop: [ALL]
      security_opt: ["no-new-privileges:true"]
      restart: unless-stopped
  ```
- Runs as **non-root**; the socket is mounted read-only into the container, and the in-process gateway restricts it to the hard-coded read endpoints. `group_add` grants the non-root user group access to the socket.
- Trade-off (documented in the README): the dashboard container now holds the read-only socket itself. The allow-list + `:ro` + all other hardening remain, but a full compromise of the app process could open its own socket connection — previously contained by the separate proxy container.
- Verified end-to-end: disallowed endpoints (`/containers`, `/info`, `/images/…/changes`, `/containers/x/kill`, …) are rejected by the gateway before any byte is sent, while the reads the app needs return 200.

### 4. README
- Quickstart (`docker compose up --build`) noting the repo ships the compose file + `Dockerfile` and the image is built from source; what each panel shows; **Security** section (in-process read-only gateway, read-only socket mount, non-root, read-only FS, dropped caps, digest pinning, optional token auth, port published on all interfaces, single-container trade-off); troubleshooting (503/permission-denied, 401, host CPU visibility); **Home Assistant** section (integration + card install, entities, options)

### 5. Home Assistant integration (`custom_components/resource_monitor/`)
Marketplace/HACS-ready custom integration that turns the monitor API into HA
sensors. Domain `resource_monitor` (the built-in `system_monitor` already owns
that name).

- **Config flow** (`config_flow.py`): URL + optional token + poll interval.
  URL is normalized (scheme assumed, trailing `/` stripped) and doubles as the
  unique ID, so the same monitor cannot be added twice. Validation GETs
  `/api/system` and maps 401/403 to `invalid_auth`, network/URL failures to
  `cannot_connect`. A **reauth** step handles a rotated `MONITOR_TOKEN`; an
  **options flow** (`OptionsFlowWithReload`) edits token + interval without
  re-adding the entry. URL is immutable on purpose — changing it would change
  every entity's unique ID.
- **Coordinator** (`coordinator.py`): `DataUpdateCoordinator` polling
  `/api/system` + `/api/containers`; interval floored at 2 s (container stats
  are expensive). Token sent as `Authorization: Bearer` first, retried with the
  dashboard-compatible `?token=` when a proxy strips the header. 401/403 →
  `ConfigEntryAuthFailed` (reauth), network/HTTP errors → `UpdateFailed`
  (retry). Docker-only failures keep the last container snapshot and surface
  the daemon error instead of flapping every sensor.
- **Sensors** (`sensor.py`): one device per monitor (`System Monitor (<host>)`,
  `configuration_url` set) with CPU %, load 1/5/15, memory total/used/
  available/%, swap total/used/%, uptime and boot time (timestamp). Per-core
  sensors track the live core count; every container becomes its own device
  (`via_device` → monitor) with CPU %, memory used/%/limit and status.
  Disappeared containers and cores are removed through `async_remove()`.
  Dynamic refresh runs behind an `asyncio.Lock` from a synchronous listener
  wrapper (the coordinator invokes listeners synchronously).
- **Branding**: `brand/icon.png` inside the integration folder (HA serves
  custom-integration brand images from there).
- **Packaging**: root `hacs.json` (`{"name": "System Monitor",
  "homeassistant": "2026.11.0"}`), `manifest.json` v0.1.0, `translations/en.json`
  alongside `strings.json`.

### 6. Lovelace card (separate repo: `etanyxyz/System-Monitor-Card`)
Marketplace requires one category per repository, so the card is its own
plugin repo rather than a folder in this one.

- Single dependency-free `resource-monitor-card.js` at the repo root (plain
  shadow DOM, no imports, no build step) + root `hacs.json` with
  `filename: resource-monitor-card.js`.
- Talks directly to `/api/system` + `/api/containers` with `fetch` (REST
  polling, default 5 s), token as `?token=` and `Authorization: Bearer`.
- UI: status dot + uptime/load header, CPU ring gauge, memory/swap bars,
  full-width per-core bars, container rows with state badge and CPU/memory.
  Container queries adapt the layout to narrow dashboard columns.
- Card editor implementing `LovelaceGenericElementEditor` (`setConfig`,
  `hass`, `config-changed` event) for a visual form instead of YAML.
- CORS in `app/main.py` (read-only `GET`/`OPTIONS`, all origins; preflights
  exempt from the token gate) is what makes this possible.

### 7. Verification performed
- Integration modules executed against the live monitor under a stubbed HA
  package: polling, payload parsing, all sensor values/devices, authed and
  unauthed flows (401 → reauth), `?token=` fallback, unreachable monitor →
  `UpdateFailed`, container-vanish and core-count changes (adds/removals),
  config-flow schema + URL normalization, options prefill.
- Card smoke-tested in Node (rendering, config editor contract, error state,
  token URL) and rendered in headless Chromium at 460/330/250 px widths and
  against a token-protected monitor.
- Container rebuilt; verified CORS headers, preflight `200`, `401` without
  token, `200` with bearer/`?token=`, and graceful `503` when the Docker
  socket is unavailable.

## Assumptions
1. **"CPU usage across threads"** = per-core/per-hardware-thread utilization (from `/proc/stat`), plus per-container CPU. True per-process-thread sampling isn't visible from inside a container (PID namespace isolation) unless run with `--pid=host`, a security trade-off that could be added later as an optional profile.
2. **Stack**: Python/FastAPI for simplicity; frontend is plain JS + Chart.js (no build step).
3. **Single container** (user decision): the socket-proxy sidecar is folded in as the in-process gateway; the container mounts the host socket read-only. The allow-list is defense-in-depth against accidental mutation, not a boundary against a fully compromised app process — stated honestly in the README.
4. **`MONITOR_TOKEN` survives** (user decision): it is the one remaining env var, optional and off by default, not set in the shipped compose file.

## Build order
1. Project scaffold (`Dockerfile`, `docker-compose.yml`, `app/`, `static/`)
2. System metrics API (RAM + per-core CPU)
3. Docker containers API
4. WebSocket push
5. Frontend dashboard
6. `docker compose up` + verify all panels against a live host
7. Socket-proxy integration: in-process gateway, `docker_stats` rewrite, single-service compose, docs, full QA (allow-list rejection, degraded/recovery cycles)
8. Home Assistant: CORS middleware, custom integration (config flow, coordinator, sensors, branding, HACS packaging), separate Lovelace card repo, docs + QA

## Git
- Integration/monitor remote: `git@github.com:etanyxyz/System-Monitor.git`
- Card remote: `git@github.com:etanyxyz/System-Monitor-Card.git` (own repo,
  local checkout at `/mnt/docker/lab/System-Monitor-Card`)
- Commits via SSH key
