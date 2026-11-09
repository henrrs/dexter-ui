"""OS-enforced worker memory scopes for synthetic generation operations."""

from __future__ import annotations

import shutil
import os
import signal
import subprocess
import sys
import time
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


def _systemd_unit(operation_id: str, attempt: int) -> str:
    if not operation_id or any(char not in "0123456789abcdef-" for char in operation_id.lower()):
        raise ValueError("operation_id must be a UUID-like identifier")
    if attempt < 0 or attempt > 99:
        raise ValueError("attempt must be between 0 and 99")
    return f"dexter-synthetic-{uuid.UUID(operation_id).hex}-a{attempt + 1:02d}-{uuid.uuid4().hex[:6]}.service"


def _systemd_unit_state(unit: str) -> str | None:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return None
    try:
        result = subprocess.run([systemctl, "--user", "show", unit.removesuffix(".service"),
                                 "--property=ActiveState", "--value"],
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def launch_memory_scoped_worker(operation_id: str, arguments: list[str], *,
                                memory_limit: int = MEMORY_LIMIT_BYTES,
                                attempt: int = 0) -> tuple[str, subprocess.Popen | None]:
    """Launch one worker attempt and return its OS handle to the outer supervisor."""
    unit = _systemd_unit(operation_id, attempt)
    runner = shutil.which("systemd-run")
    if runner:
        project_src = str(Path(__file__).resolve().parents[2])
        python_path = os.pathsep.join(filter(None, (project_src, os.environ.get("PYTHONPATH", ""))))
        command = [runner, "--user", f"--unit={unit.removesuffix('.service')}", "--quiet",
                   f"--property=MemoryMax={memory_limit}", "--property=MemorySwapMax=0",
                   "--property=OOMPolicy=kill", "--property=KillMode=control-group",
                   "--property=MemoryAccounting=yes", "--property=TimeoutStopSec=5s",
                   f"--property=WorkingDirectory={Path.cwd()}", f"--setenv=PYTHONPATH={python_path}", "--",
                   sys.executable, *arguments]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        except subprocess.TimeoutExpired as exc:
            state = _systemd_unit_state(unit)
            if state in {"active", "activating", "deactivating"}:
                return unit, None
            stop_memory_scoped_worker(unit)
            raise RuntimeError("systemd worker launch timed out; no fallback worker was started") from exc
        except OSError:
            result = None
        if result is not None and result.returncode == 0:
            return unit, None
        if result is not None and _systemd_unit_state(unit) is not None:
            return unit, None
    if not Path("/proc/self/status").is_file():
        raise RuntimeError("No safe isolated worker runtime is available")
    # Best-effort fallback: process isolation plus RSS guard, with no hard-cap claim.
    environment = dict(os.environ)
    environment["DEXTER_SYNTHETIC_RSS_GUARD"] = "1"
    process = subprocess.Popen([sys.executable, *arguments], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        close_fds=True, env=environment)
    return f"dexter-pid-{process.pid}", process


def start_memory_scoped_worker(operation_id: str, arguments: list[str], *,
                               memory_limit: int = MEMORY_LIMIT_BYTES, attempt: int = 0) -> str:
    """Compatibility wrapper for starting one capped synthetic worker."""
    unit, _process = launch_memory_scoped_worker(operation_id, arguments,
                                                  memory_limit=memory_limit, attempt=attempt)
    return unit


def start_synthetic_supervisor(arguments: list[str]) -> str:
    """Start a small uncapped controller; only its child worker enters the cgroup."""
    project_src = str(Path(__file__).resolve().parents[2])
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (project_src, environment.get("PYTHONPATH", ""))))
    process = subprocess.Popen([sys.executable, "-m", "dexter.services.synthetic_supervisor", *arguments],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True, env=environment)
    time.sleep(0.05)
    if process.poll() is not None:
        process.wait()
        raise RuntimeError("The synthetic supervisor exited during startup")
    return f"dexter-supervisor-pid-{process.pid}"


def stop_memory_scoped_worker(unit: str) -> None:
    if unit.startswith("dexter-pid-"):
        try:
            process_id = int(unit.removeprefix("dexter-pid-"))
            os.killpg(process_id, signal.SIGTERM)
            if not Path(f"/proc/{process_id}").exists():
                return
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.killpg(process_id, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.1)
            os.killpg(process_id, signal.SIGKILL)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        return
    unit_name = unit.removesuffix(".service")
    if not unit_name.startswith("dexter-synthetic-") or any(char not in "0123456789abcdef-a" for char in unit_name.removeprefix("dexter-synthetic-")):
        raise ValueError("invalid Dexter synthetic worker unit")
    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise RuntimeError("systemctl is unavailable")
    try:
        result = subprocess.run([systemctl, "--user", "stop", unit_name],
                                capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or result.returncode:
        try:
            subprocess.run([systemctl, "--user", "kill", "--kill-who=all", "--signal=KILL", unit_name],
                           capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass


def stop_synthetic_supervisor(unit: str) -> None:
    if not unit.startswith("dexter-supervisor-pid-"):
        raise ValueError("invalid Dexter synthetic supervisor id")
    try:
        process_id = int(unit.removeprefix("dexter-supervisor-pid-"))
        os.killpg(process_id, signal.SIGTERM)
        if not Path(f"/proc/{process_id}").exists():
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(process_id, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.killpg(process_id, signal.SIGKILL)
    except (ValueError, ProcessLookupError, PermissionError):
        pass


def wait_memory_scoped_worker(unit: str, process: subprocess.Popen | None,
                              *, poll_seconds: float = 0.5) -> dict[str, Any]:
    """Wait outside the worker cgroup and retain systemd's OOM result evidence."""
    if process is not None:
        return {"exit_code": process.wait(), "result": None, "signal": None}
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {"exit_code": None, "result": "unknown", "signal": None}
    unit_name = unit.removesuffix(".service")
    while True:
        try:
            result = subprocess.run([systemctl, "--user", "show", unit_name,
                                     "--property=ActiveState", "--property=Result",
                                     "--property=ExecMainStatus"],
                                    capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {"exit_code": None, "result": "unknown", "signal": None}
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        state = values.get("ActiveState", "")
        if result.returncode != 0:
            return {"exit_code": None, "result": "unknown", "signal": None}
        if state in {"inactive", "failed"}:
            try:
                code = int(values.get("ExecMainStatus", "0"))
            except ValueError:
                code = None
            outcome = {"exit_code": code, "result": values.get("Result", "unknown"), "signal": None}
            subprocess.run([systemctl, "--user", "reset-failed", unit_name],
                           capture_output=True, text=True, timeout=5, check=False)
            return outcome
        time.sleep(poll_seconds)


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


def report_runtime_memory_metrics() -> dict[str, int | None]:
    """Read cgroup-v2 usage/peak counters when available and always include RSS."""
    metrics: dict[str, int | None] = {"rss_bytes": report_runtime_rss(), "cgroup_current_bytes": None,
                                     "cgroup_peak_bytes": None, "cgroup_limit_bytes": None,
                                     "cgroup_oom_events": None, "cgroup_oom_kill_events": None}
    try:
        relative = next(line.split(":", 2)[2].strip() for line in Path("/proc/self/cgroup").read_text().splitlines()
                        if line.startswith("0::"))
        if relative.startswith("/") and ".." not in Path(relative).parts:
            scope = Path("/sys/fs/cgroup") / relative.lstrip("/")
            for key, filename in (("cgroup_current_bytes", "memory.current"),
                                  ("cgroup_peak_bytes", "memory.peak"),
                                  ("cgroup_limit_bytes", "memory.max")):
                raw = (scope / filename).read_text().strip()
                metrics[key] = int(raw) if raw.isdigit() else None
            events = {}
            for line in (scope / "memory.events").read_text().splitlines():
                name, value = line.split()
                events[name] = int(value)
            metrics["cgroup_oom_events"] = events.get("oom")
            metrics["cgroup_oom_kill_events"] = events.get("oom_kill")
    except (OSError, ValueError, StopIteration):
        pass
    return metrics
