"""Docker container inventory and stats.

Talks to the Engine API through the in-process read-only gateway
(`app.docker_gateway`), which fronts the mounted read-only
/var/run/docker.sock and only permits a hard-coded set of GET endpoints.
Degrades gracefully: when the daemon is unavailable, get_containers()
returns {"error": msg} instead of raising.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import docker_gateway as gw
from .docker_gateway import GatewayError

_image_tags: dict[str, str] = {}

# How many running containers to sample per pass, and how many stats
# calls to run at once. A single non-streaming stats call takes ~1s on
# the daemon (its own internal sampling window) but already includes
# both precpu_stats and cpu_stats, so one call per container suffices.
_MAX_SAMPLED = 25
_MAX_WORKERS = 8


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


def _ports_from_row(row: dict) -> str:
    """Port mappings the way `docker ps` shows them (e.g. 0.0.0.0:1010->1010/tcp)."""
    parts = []
    for p in row.get("Ports") or []:
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


def _health_from_row(row: dict) -> str | None:
    """Health status, or None when the container has no healthcheck."""
    health = (row.get("Health") or {}).get("Status")
    return None if health in (None, "none") else health


def get_containers() -> dict:
    """Return {"containers": [...]} or {"error": msg}.

    Live CPU % and memory figures are sampled only for running
    containers (capped at _MAX_SAMPLED per pass). The `docker ps` fields
    come from the single /containers/json list call; only exited
    containers need an extra inspect, to recover the exit code.
    """
    try:
        rows = gw.list_containers()
    except GatewayError as exc:
        return {"error": str(exc)}

    def image_tag(ref: str | None) -> str | None:
        """Resolve an image reference (often a sha256 digest) to its first tag."""
        if not ref:
            return ref
        if ref in _image_tags:
            return _image_tags[ref]
        try:
            tags = (gw.inspect_image(ref) or {}).get("RepoTags") or []
            _image_tags[ref] = tags[0] if tags else ref
        except GatewayError:
            _image_tags[ref] = ref
        return _image_tags[ref]

    result = []
    running_ids: list[str] = []
    for row in rows:
        cid = row.get("Id") or ""
        state = row.get("State") or ""  # running/exited/paused/created/dead
        entry = {
            "id": cid[:12],
            "name": (row.get("Names") or ["/"])[0].lstrip("/"),
            "image": image_tag(row.get("Image")),
            # formatted the way `docker ps` shows it
            "command": row.get("Command") or "",
            "state": state,
            # docker-ps-style status, e.g. "Up 3 hours (healthy)";
            # falls back to the raw state word if absent
            "status": row.get("Status") or state or "unknown",
            "health": _health_from_row(row),
            "created": row.get("Created"),
            "ports": _ports_from_row(row),
            "labels": row.get("Labels") or {},
            "exit_code": None,
            "cpu_percent": None,
            "memory_usage_bytes": None,
            "memory_limit_bytes": None,
            "memory_percent": None,
        }
        result.append(entry)
        if state.lower().startswith("running"):
            running_ids.append(cid)
        else:
            # The exit code lives only in the full inspect (State.ExitCode);
            # non-running containers are few, so one inspect each is cheap.
            try:
                entry["exit_code"] = (gw.inspect_container(cid) or {}).get("State", {}).get(
                    "ExitCode"
                )
            except GatewayError:
                pass

    running_ids = running_ids[:_MAX_SAMPLED]

    def _fetch_stats(cid: str):
        try:
            return cid, gw.container_stats(cid)
        except GatewayError:
            return cid, None

    # Each stats call takes ~1s on the daemon, so run them concurrently;
    # a pass is ~1s total instead of ~1s per container.
    if running_ids:
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(running_ids))) as pool:
            for cid, stats in pool.map(_fetch_stats, running_ids):
                entry = next((r for r in result if r["id"] == cid[:12]), None)
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
