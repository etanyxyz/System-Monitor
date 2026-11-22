# System-Monitor

Containerized resource monitor with a live web dashboard:

- **Host RAM usage** — total/used/free/available/swap (from `/proc/meminfo`)
- **CPU usage across threads** — per-core/per-hardware-thread utilization (from `/proc/stat`)
- **Docker containers** — every `docker ps` field (ID, image, command, created, status, ports, name, labels) plus live CPU % and memory
- **Web dashboard** — served at `http://localhost:1010`

## Quickstart

The monitor ships as a single Docker image, `etanyxyz/system-monitor`, and
deploys as a single hardened container via this compose file:

```yaml
services:
  monitor:
    image: etanyxyz/system-monitor:latest
    container_name: resource-monitor
    ports:
      - "1010:1010"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    group_add:
      - "985"   # host docker GID — set to `getent group docker`'s GID
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    restart: unless-stopped
```

Open <http://localhost:1010>.

The container mounts the host Docker socket read-only. The app runs as the
non-root `monitor` user, so it joins the host's `docker` group at start
(`group_add` — `985` in the example above; set it to the GID from
`getent group docker` on your host). No environment variables are set:
the only optional one is `MONITOR_TOKEN` (see Security), which you add to
the compose file yourself if you want bearer auth.

To build the image from source instead of pulling it, run
`docker build -t etanyxyz/system-monitor:latest .` in this repo (the
repo's own `docker-compose.yml` does exactly that plus deployment in one
step: `docker compose up --build`).

### Dashboard

- **CPU / Memory cards** — overall utilization with live sparklines
- **Timeline** — CPU and RAM utilization over time (5 min / 15 min / 1 hour
  windows) plus a per-container CPU chart
- **CPU by core** — per-core (per hardware thread) utilization, color-coded
  green/yellow/red
- **Memory breakdown** — stacked used/cached/buffers/free bar plus swap totals
- **Containers** — name, image, state badge, health, uptime, live CPU % and
  memory used/limit

Data arrives over a WebSocket (1.5 s pushes, container stats every ~6 s); the
page falls back to HTTP polling if the socket connection drops. Static assets
are served with `no-cache` so a refreshed page always runs the latest frontend.

## How it works

The monitor is a single Docker container running FastAPI + Uvicorn:

- Host CPU/RAM are read from `/proc/stat` and `/proc/meminfo`. These files
  are host-wide, so no privileged mode or extra mounts are needed.
- Docker container data comes from the Engine API through an **in-process
  read-only gateway** (`app/docker_gateway.py`). It speaks the Engine API
  directly over the read-only-mounted `/var/run/docker.sock` (plain
  HTTP/1.1 over the unix socket, stdlib only — no docker-py), but only ever
  issues a hard-coded set of `GET` endpoints: `/_ping`, `/version`,
  `/containers/json`, `/containers/{id}/json`, `/containers/{id}/stats`,
  `/images/{ref}/json`. Anything else is rejected before a byte reaches the
  daemon (see Security).

### API

| Endpoint | Description |
|---|---|
| `GET /api/system` | Host CPU (overall + per core), memory, load averages, uptime |
| `GET /api/containers` | Container list with state, status, health, live CPU % and memory |
| `WS /ws` | Pushes a snapshot every ~1.5 s (containers refreshed every ~6 s) |

## Security

The container is hardened so a compromise of the dashboard can't touch your
Docker daemon or host:

| Control | Detail |
|---|---|
| Read-only Docker access | The in-process gateway (`app/docker_gateway.py`) only ever issues the hard-coded `GET` endpoints `/_ping`, `/version`, `/containers/json`, `/containers/{id}/json`, `/containers/{id}/stats`, `/images/{ref}/json`. It can list containers and read stats — it **cannot** create, start, stop, kill, delete, exec, build, or otherwise mutate anything; every other request is rejected client-side before it is sent. |
| Read-only socket mount | `/var/run/docker.sock` is mounted into the container with `:ro`. The app runs as the unprivileged `monitor` user (group membership via compose `group_add`) so it can only connect to the socket, never modify it. |
| Non-root process | The app runs as the unprivileged `monitor` user. |
| Read-only filesystems | The container runs with `read_only: true` (tmpfs only for scratch). |
| No capabilities | `cap_drop: ALL` + `no-new-privileges`. |
| Pinned image | Base image pinned by digest; Python deps pinned exactly in `requirements.txt`. |
| Optional auth | Add `environment: [MONITOR_TOKEN=<secret>]` to require a token on every request (API, WebSocket, static files). Open the dashboard as `http://host:1010/?token=<secret>`. Off by default. |

Honest trade-off of the single-container design: the container that serves
the dashboard also holds the (read-only) Docker socket. The in-process
allow-list still blocks every mutation endpoint, and the `:ro` mount,
non-root user, read-only FS, and dropped capabilities all remain — but a
*full* compromise of the app process could open its own connection to the
mounted socket, which a separate socket-proxy container previously
contained. If you need that extra boundary, run the monitor behind an
external read-only proxy or restrict the dashboard port to the host.

Notes:

- The dashboard port (`1010`) is published on all interfaces. If you only
  use it on the host, change the compose port to `127.0.0.1:1010:1010`.

## Troubleshooting

- **`/api/containers` returns 503** — the app can't reach the daemon. Check
  `docker logs resource-monitor`, that `docker` works on the host, that the
  socket mount is present, and that `group_add` matches the host's `docker`
  GID (`getent group docker`) — a wrong GID shows up as a *Permission
  denied* error.
- **`/` or `/api/*` returns 401** — `MONITOR_TOKEN` is set; open the page
  with `?token=<secret>` in the URL (and use the same token in API calls).
- **CPU numbers look low** — per-core utilization is sampled between pushes;
  idle hosts report near-zero values by design.
