# System-Monitor

Containerized resource monitor with a live web dashboard:

- **Host RAM usage** — total/used/free/available/swap (from `/proc/meminfo`)
- **CPU usage across threads** — per-core/per-hardware-thread utilization (from `/proc/stat`)
- **Docker containers** — every `docker ps` field (ID, image, command, created, status, ports, name, labels) plus live CPU % and memory
- **Web dashboard** — served at `http://localhost:1010`

## Quickstart

The repo ships a hardened `docker-compose.yml` (monitor + read-only
socket-proxy) and a `Dockerfile`; the image is built from source on first
run:

```bash
docker compose up --build
```

Open <http://localhost:1010>.

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
- Docker container data comes from the Engine API through a **read-only
  socket proxy** (`lscr.io/linuxserver/socket-proxy`) that fronts
  `/var/run/docker.sock`. The monitor container itself never sees the
  socket — it connects to `tcp://socket-proxy:2375`, which only allows
  `GET /_ping`, `/version`, `/containers/*` and `/images/*` (see Security).

### API

| Endpoint | Description |
|---|---|
| `GET /api/system` | Host CPU (overall + per core), memory, load averages, uptime |
| `GET /api/containers` | Container list with state, status, health, live CPU % and memory |
| `WS /ws` | Pushes a snapshot every ~1.5 s (containers refreshed every ~6 s) |

## Security

The compose stack is hardened so a compromise of the dashboard can't touch
your Docker daemon or host:

| Control | Detail |
|---|---|
| Read-only Docker access | The app reaches the daemon only through the `socket-proxy` container with `POST=0` (GET/HEAD only) and just `PING`, `VERSION`, `CONTAINERS`, `IMAGES` enabled. It can list containers and read stats — it **cannot** create, start, stop, kill, delete, exec, build, or otherwise mutate anything. |
| No socket in the app | `/var/run/docker.sock` is mounted (read-only) only into `socket-proxy`, never into the monitor. The monitor talks to it over the compose network at `tcp://socket-proxy:2375`, and the proxy port is not published. |
| Non-root process | The monitor runs as the unprivileged `monitor` user. |
| Read-only filesystems | Both containers run with `read_only: true` (tmpfs only for scratch). |
| No capabilities | `cap_drop: ALL` + `no-new-privileges` on both containers. |
| Pinned images | Base images are pinned by digest; Python deps are pinned exactly in `requirements.txt`. |
| Optional auth | Set `MONITOR_TOKEN=<secret>` for the monitor service to require a token on every request (API, WebSocket, static files). Open the dashboard as `http://host:1010/?token=<secret>`. Off by default. |

Notes:

- The dashboard port (`1010`) is published on all interfaces. If you only
  use it on the host, change the compose port to `127.0.0.1:1010:1010`.
- The proxy container is trusted (it holds the socket), so keep it
  digest-pinned as shipped and don't expose its port 2375.

## Troubleshooting

- **`/api/containers` returns 503** — the proxy can't reach the daemon or
  the endpoint it needs. Check `docker logs docker-socket-proxy`, that
  `docker` works on the host, and that the socket mount is present.
- **`/` or `/api/*` returns 401** — `MONITOR_TOKEN` is set; open the page
  with `?token=<secret>` in the URL (and use the same token in API calls).
- **CPU numbers look low** — per-core utilization is sampled between pushes;
  idle hosts report near-zero values by design.
