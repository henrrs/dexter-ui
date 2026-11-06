"""Local analytics resource caps read from Dexter's local config directory."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AnalyticsLimits:
    concurrent: int = 2
    queued: int = 2
    memory_limit: str = "1GB"
    minimum_free_disk_mb: int = 256


def load_limits(config_dir: Path) -> AnalyticsLimits:
    """Read optional analytics.json; invalid fields fall back independently to safe defaults."""
    defaults = AnalyticsLimits()
    try:
        data = json.loads((config_dir / "analytics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    concurrent = data.get("max_concurrent", defaults.concurrent)
    queued = data.get("max_queued", defaults.queued)
    memory = data.get("memory_limit", defaults.memory_limit)
    disk = data.get("minimum_free_disk_mb", defaults.minimum_free_disk_mb)
    concurrent = concurrent if isinstance(concurrent, int) and 1 <= concurrent <= 4 else defaults.concurrent
    queued = queued if isinstance(queued, int) and 0 <= queued <= 16 else defaults.queued
    valid_memory = isinstance(memory, str) and re.fullmatch(r"[1-9][0-9]{0,3}(?:MB|GB)", memory)
    if valid_memory:
        amount, unit = int(memory[:-2]), memory[-2:]
        valid_memory = amount <= (65536 if unit == "MB" else 64)
    memory = memory if valid_memory else defaults.memory_limit
    disk = disk if isinstance(disk, int) and 128 <= disk <= 1_048_576 else defaults.minimum_free_disk_mb
    return AnalyticsLimits(concurrent, queued, memory, disk)
