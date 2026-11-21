"""Docker container inventory and stats via the Engine API.

Requires access to the Docker socket (mounted by docker-compose.yml).
Degrades gracefully: when the socket is unavailable, get_containers()
returns {"error": msg} instead of raising.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import docker
from docker.errors import DockerException

_client: docker.DockerClient | None = None
_image_tags: dict[str, str] = {}

# How many running containers to sample per pass, and how many stats
# calls to run at once. A single non-streaming stats call takes ~1s on
# the daemon (its own internal sampling window) but already includes
# both precpu_stats and cpu_stats, so one call per container suffices.
_MAX_SAMPLED = 25
_MAX_WORKERS = 8


def _get_client() -> docker.DockerClient:
    """Return a live Docker client, or raise ConnectionError."""
    global _client
    if _client is not None:
        try:
            _client.ping()
            return _client
        except DockerException:
            _client = None
    try:
        _client = docker.from_env()  # honors DOCKER_HOST, else unix socket
        _client.ping()
        return _client
    except (DockerException, OSError) as exc:
        raise ConnectionError(f"cannot reach Docker daemon: {exc}") from exc


def _cpu_percent_from_stats(stats: dict) -> float:
    """Container CPU utilization (%) from one non-streaming stats message.

    The daemon samples for ~1s internally and returns both the before
    (precpu_stats) and after (cpu_stats) snapshots, so a single call is
    enough for a meaningful short-window CPU %.
    """
    try:
        after = stats["cpu_stats"]
        before = stats.get("precpu_stats", {})
        cpu_delta = (
            after["cpu_usage"]["total_usage"]
            - before.get("cpu_usage", {}).get("total_usage", 0)
        )
        sys_delta = after["system_cpu_usage"] - before.get("system_cpu_usage", 0)
        per_cpu = after["cpu_usage"].get("per_cpu_usage") or []
        cores = len(per_cpu) or 1
        if cpu_delta <= 0 or sys_delta <= 0:
            return 0.0
        return (cpu_delta / sys_delta) * cores * 100
    except (KeyError, TypeError):
        return 0.0


def _memory_from_stats(after: dict) -> tuple[float, float]:
    try:
        usage = float(after["memory_stats"]["usage"] or 0)
        limit = float(after["memory_stats"]["limit"] or 0)
    except (KeyError, TypeError):
        return 0.0, 0.0
    return usage, limit


def get_containers() -> dict:
    """Return {"containers": [...]} or {"error": msg}.

    Live CPU % and memory figures are sampled only for running
    containers (capped at _MAX_SAMPLED per pass).
    """
    try:
        client = _get_client()
        containers = client.containers.list(all=True)
    except (ConnectionError, DockerException) as exc:
        return {"error": str(exc)}

    def image_tag(ref: str | None) -> str | None:
        """Resolve an image reference (often a sha256 digest) to its first tag."""
        if not ref:
            return ref
        if ref in _image_tags:
            return _image_tags[ref]
        try:
            tags = client.images.get(ref).tags
            _image_tags[ref] = tags[0] if tags else ref
        except DockerException:
            _image_tags[ref] = ref
        return _image_tags[ref]

    result = []
    for c in containers:
        state = c.attrs.get("State", {})
        health = (state.get("Health") or {}).get("Status")
        entry = {
            "id": c.short_id,
            "name": c.name,
            "image": image_tag(c.attrs.get("Image")),
            "state": state.get("Status"),  # running/exited/paused/created/dead
            "status": c.status,  # e.g. "Up 3 hours"
            "health": health,  # healthy/unhealthy/starting, or None
            "created": c.attrs.get("Created"),
            "exit_code": state.get("ExitCode"),
            "cpu_percent": None,
            "memory_usage_bytes": None,
            "memory_limit_bytes": None,
            "memory_percent": None,
        }
        result.append(entry)

    running = [c for c in containers if c.status.lower().startswith("running")][
        :_MAX_SAMPLED
    ]

    def _fetch_stats(c):
        try:
            return c.short_id, c.stats(stream=False)
        except DockerException:
            return c.short_id, None

    # Each stats call takes ~1s on the daemon, so run them concurrently;
    # a pass is ~1s total instead of ~1s per container.
    if running:
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(running))) as pool:
            for cid, stats in pool.map(_fetch_stats, running):
                entry = next((r for r in result if r["id"] == cid), None)
                if entry is None or stats is None:
                    continue
                entry["cpu_percent"] = round(_cpu_percent_from_stats(stats), 1)
                usage, limit = _memory_from_stats(stats)
                entry["memory_usage_bytes"] = usage
                entry["memory_limit_bytes"] = limit
                entry["memory_percent"] = (
                    round(usage / limit * 100, 1) if limit > 0 else None
                )

    return {"containers": result}
