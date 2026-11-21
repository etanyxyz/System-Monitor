# System-Monitor

Containerized resource monitor with a live web dashboard:

- **Host RAM usage** — total/used/free/available/swap (from `/proc/meminfo`)
- **CPU usage across threads** — per-core/per-hardware-thread utilization (from `/proc/stat`)
- **Docker containers** — inventory, state, status, health, live CPU % and memory
- **Web dashboard** — served at `http://localhost:8080`

## Quickstart

```bash
docker compose up --build
```

Open <http://localhost:8080>.

### Dashboard

- **CPU / Memory cards** — overall utilization with a live CPU sparkline and a
  used/cached/buffers/free RAM bar
- **CPU by core** — per-core (per hardware thread) utilization, color-coded
  green/yellow/red
- **Memory breakdown** — stacked used/cached/buffers/free bar plus swap totals
- **Containers** — name, image, state badge, health, uptime, live CPU % and
  memory used/limit

Data arrives over a WebSocket (1.5 s pushes, container stats every ~6 s); the
page falls back to HTTP polling if the socket connection drops.

## How it works

The monitor is a single Docker container running FastAPI + Uvicorn:

- Host CPU/RAM are read from `/proc/stat` and `/proc/meminfo`. These files
  are host-wide, so no privileged mode or extra mounts are needed.
- Docker container data comes from the Engine API via the mounted
  `/var/run/docker.sock` (same approach as Portainer).

### API

| Endpoint | Description |
|---|---|
| `GET /api/system` | Host CPU (overall + per core), memory, load averages, uptime |
| `GET /api/containers` | Container list with state, status, health, live CPU % and memory |
| `WS /ws` | Pushes a snapshot every ~1.5 s (containers refreshed every ~6 s) |

## Permissions

Mounting `/var/run/docker.sock` grants the container **full access to the
Docker daemon** (it can inspect, start, and stop containers). Only run this
on hosts you trust. The container runs as root so it can read the socket,
whose group GID varies per host.

## Troubleshooting

- **`/api/containers` returns 503** — the socket is missing or not readable.
  Check the compose volume mount and that `docker` works on the host.
- **CPU numbers look low** — per-core utilization is sampled between pushes;
  idle hosts report near-zero values by design.
