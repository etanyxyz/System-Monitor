"""Docker container inventory and stats via the Engine API.

Requires access to the Docker socket (mounted by docker-compose.yml).
Degrades gracefully: when the socket is unavailable, get_containers()
returns {"error": msg} instead of raising.
"""

from __future__ import annotations

import time

import docker
from docker.errors import DockerException

_client: docker.DockerClient | None = None
_image_tags: dict[str, str] = {}

# How long to sample a container's stats for, and how many running
# containers to sample per pass (each sample costs two daemon round trips).
_SAMPLE_INTERVAL = 0.5
_MAX_SAMPLED = 25


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


def _cpu_percent_from_stats(before: dict, after: dict) -> float:
    """Container CPU utilization (%) between two /stats snapshots."""
    try:
        cpu_delta = (
            after["cpu_stats"]["cpu_usage"]["total_usage"]
            - before["cpu_stats"]["cpu_usage"]["total_usage"]
        )
        sys_delta = (
            after["cpu_stats"]["system_cpu_usage"]
            - before["cpu_stats"]["system_cpu_usage"]
        )
        per_cpu = after["cpu_stats"]["cpu_usage"].get("per_cpu_usage") or []
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

    running = [c for c in containers if c.status.lower().startswith("running")]
    for c in running[:_MAX_SAMPLED]:
        try:
            before = c.stats(stream=False)
            time.sleep(_SAMPLE_INTERVAL)
            after = c.stats(stream=False)
        except DockerException:
            continue
        entry = next((r for r in result if r["id"] == c.short_id), None)
        if entry is None:
            continue
        entry["cpu_percent"] = round(_cpu_percent_from_stats(before, after), 1)
        usage, limit = _memory_from_stats(after)
        entry["memory_usage_bytes"] = usage
        entry["memory_limit_bytes"] = limit
        entry["memory_percent"] = round(usage / limit * 100, 1) if limit > 0 else None

    return {"containers": result}
