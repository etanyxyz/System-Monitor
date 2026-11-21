"""Docker container inventory and stats via the Engine API.

Reaches the daemon through DOCKER_HOST (a read-only socket-proxy in the
shipped compose file; a mounted /var/run/docker.sock also works).
Degrades gracefully: when the daemon is unavailable, get_containers()
returns {"error": msg} instead of raising.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import docker
from docker.errors import DockerException
from requests.exceptions import RequestException

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
        # docker-py leaks connection-level failures as
        # requests.exceptions.ConnectionError (not a DockerException),
        # so catch request errors too — otherwise a dead daemon strands
        # a stale client here and every call below raises uncaught.
        except (DockerException, RequestException, OSError):
            _client = None
    try:
        _client = docker.from_env()  # honors DOCKER_HOST, else unix socket
        _client.ping()
        return _client
    except (DockerException, RequestException, OSError) as exc:
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


def _command_from_attrs(attrs: dict) -> str:
    """Container command the way `docker ps` shows it."""
    cmd = (attrs.get("Config") or {}).get("Cmd") or attrs.get("Args") or []
    return " ".join(cmd)


def _ports_from_attrs(attrs: dict) -> str:
    """Port mappings the way `docker ps` shows them (e.g. 0.0.0.0:1010->1010/tcp)."""
    parts = []
    for p in attrs.get("Ports") or []:
        priv = p.get("PrivatePort")
        if not priv:
            continue
        pub = p.get("PublicPort")
        proto = p.get("Type", "tcp")
        ip = p.get("IP") or ""
        if ip and ":" in ip:  # bracket IPv6, docker-style
            ip = f"[{ip}]"
        if pub:
            parts.append(f"{ip}:{pub}->{priv}/{proto}" if ip else f"{pub}->{priv}/{proto}")
        else:
            parts.append(f"{ip}:{priv}/{proto}" if ip else f"{priv}/{proto}")
    return ", ".join(parts)


def get_containers() -> dict:
    """Return {"containers": [...]} or {"error": msg}.

    Live CPU % and memory figures are sampled only for running
    containers (capped at _MAX_SAMPLED per pass).
    """
    try:
        client = _get_client()
        containers = client.containers.list(all=True)
        # Raw list endpoint (the one `docker ps` is built from): provides
        # the human-readable Status, Ports and Command strings that the
        # inspect responses (behind containers.list) don't carry.
        ps_rows = {r["Id"]: r for r in client.api.containers(all=True)}
    except (ConnectionError, DockerException, RequestException) as exc:
        # covers a daemon that dies mid-pass (requests-level errors) as
        # well as the ConnectionError raised by _get_client()
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
        ps_row = ps_rows.get(c.id, {})
        state = c.attrs.get("State", {})
        health = (state.get("Health") or {}).get("Status")
        entry = {
            "id": c.short_id,
            "name": c.name,
            "image": image_tag(c.attrs.get("Image")),
            # formatted the way `docker ps` shows it
            "command": ps_row.get("Command") or _command_from_attrs(c.attrs),
            "state": state.get("Status"),  # running/exited/paused/created/dead
            # docker-ps-style status, e.g. "Up 3 hours (healthy)";
            # falls back to the raw state word if absent
            "status": ps_row.get("Status") or c.status,
            "health": health,  # healthy/unhealthy/starting, or None
            "created": c.attrs.get("Created"),
            "ports": _ports_from_attrs(ps_row),
            "labels": (c.attrs.get("Config") or {}).get("Labels")
            or ps_row.get("Labels") or {},
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
