"""Host-level CPU and memory metrics.

Reads /proc-based stats via psutil. /proc/stat and /proc/meminfo are
host-wide (not PID-namespace scoped), so a plain container can read
host stats with no special privileges or mounts.
"""

from __future__ import annotations

import os
import time

import psutil


class CpuSampler:
    """Per-core CPU usage from consecutive /proc/stat samples.

    psutil.cpu_percent(interval=None, percpu=True) returns the change
    since the previous call, so it is primed once and each subsequent
    call yields utilization for the interval in between.
    """

    def __init__(self) -> None:
        self._primed = False

    def prime(self) -> None:
        if not self._primed:
            psutil.cpu_percent(percpu=True)
            self._primed = True

    def sample(self) -> list[float]:
        """Utilization (%) per core/thread since the previous call."""
        self.prime()
        return psutil.cpu_percent(interval=None, percpu=True)


_sampler = CpuSampler()


def get_system() -> dict:
    """Snapshot of host CPU, memory, load averages, and uptime."""
    per_core = _sampler.sample()
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = (0.0, 0.0, 0.0)

    boot_time = psutil.boot_time()

    return {
        "cpu": {
            "overall_percent": round(sum(per_core) / len(per_core), 1) if per_core else 0.0,
            "per_core_percent": [round(v, 1) for v in per_core],
            "cores": len(per_core),
            "load_avg": [round(v, 2) for v in load],
        },
        "memory": {
            "total_bytes": mem.total,
            "used_bytes": mem.used,
            "free_bytes": mem.free,
            "available_bytes": mem.available,
            "percent": mem.percent,
            "swap_total_bytes": swap.total,
            "swap_used_bytes": swap.used,
            "swap_percent": swap.percent,
        },
        "uptime_seconds": int(time.time() - boot_time),
        "boot_time": boot_time,
        "timestamp": time.time(),
    }
