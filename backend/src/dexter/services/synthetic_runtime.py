"""OS-enforced worker memory scopes for synthetic generation operations."""

from __future__ import annotations

import shutil
import os
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

MEMORY_LIMIT_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RuntimeCapability:
    available: bool
    mechanism: str | None
    message: str


def probe_memory_scope(*, timeout: float = 8.0) -> RuntimeCapability:
    """Probe a real transient systemd scope instead of treating DuckDB's cap as RSS."""
    if not sys.platform.startswith("linux"):
        return RuntimeCapability(False, None, "A Linux cgroup memory scope is required for synthetic publication.")
    runner = shutil.which("systemd-run")
    true = shutil.which("true")
    if runner and true:
        command = [runner, "--user", "--scope", "--quiet", "--wait",
                   f"--property=MemoryMax={MEMORY_LIMIT_BYTES}", "--", true]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            return RuntimeCapability(True, "systemd-user-cgroup-v2", "The aggregate 256 MiB worker memory limit is enforceable.")
    if Path("/proc/self/status").is_file():
        return RuntimeCapability(True, "isolated-process-rss-guard", "A cgroup cap is unavailable. Dexter will isolate the worker, reduce DuckDB memory, monitor RSS, and stop it when the safety threshold is reached; the 256 MiB ceiling is not guaranteed.")
    return RuntimeCapability(False, None, "Neither a cgroup memory limit nor Linux process RSS monitoring is available.")


def start_memory_scoped_worker(operation_id: str, arguments: list[str], *, memory_limit: int = MEMORY_LIMIT_BYTES) -> str:
    """Start an isolated, transient user service without passing credentials in argv."""
    if not operation_id or any(char not in "0123456789abcdef-" for char in operation_id.lower()):
        raise ValueError("operation_id must be a UUID-like identifier")
    unit = f"dexter-synthetic-{uuid.UUID(operation_id).hex}"
    runner = shutil.which("systemd-run")
    if runner:
        project_src = str(Path(__file__).resolve().parents[2])
        python_path = os.pathsep.join(filter(None, (project_src, os.environ.get("PYTHONPATH", ""))))
        command = [runner, "--user", f"--unit={unit}", "--collect", "--quiet",
                   f"--property=MemoryMax={memory_limit}", "--property=MemorySwapMax=0",
                   "--property=OOMPolicy=kill", "--property=KillMode=control-group",
                   f"--property=WorkingDirectory={Path.cwd()}", f"--setenv=PYTHONPATH={python_path}", "--",
                   sys.executable, *arguments]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            return unit
    if not Path("/proc/self/status").is_file():
        raise RuntimeError("No safe isolated worker runtime is available")
    # Best-effort fallback: process isolation plus RSS guard, with no hard-cap claim.
    environment = dict(os.environ)
    environment["DEXTER_SYNTHETIC_RSS_GUARD"] = "1"
    process = subprocess.Popen([sys.executable, *arguments], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        close_fds=True, env=environment)
    return f"dexter-pid-{process.pid}"


def stop_memory_scoped_worker(unit: str) -> None:
    if unit.startswith("dexter-pid-"):
        try:
            process_id = int(unit.removeprefix("dexter-pid-"))
            os.killpg(process_id, signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        return
    if not unit.startswith("dexter-synthetic-") or any(char not in "0123456789abcdef-" for char in unit.removeprefix("dexter-synthetic-").removesuffix(".service")):
        raise ValueError("invalid Dexter synthetic worker unit")
    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise RuntimeError("systemctl is unavailable")
    subprocess.run([systemctl, "--user", "kill", "--kill-who=all", "--signal=TERM", unit],
                   capture_output=True, text=True, timeout=10, check=False)


def runtime_entrypoint() -> list[str]:
    return ["-m", "dexter.services.synthetic_worker"]


def report_runtime_rss() -> int | None:
    """Return this worker process RSS from procfs, where available."""
    status = Path("/proc/self/status")
    try:
        for line in status.read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None
