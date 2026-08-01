"""macOS LaunchAgent installation and content-free health inspection."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from founder_os.models import UTC, parse_datetime, utc_now
from founder_os.paths import ensure_private_directory, state_root


LAUNCH_AGENT_LABEL = "com.founderos.runtime"
EMULATOR_LAUNCH_AGENT_LABEL = "com.founderos.busybar-emulator"


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    loaded: bool
    pid: int | None
    state: str
    health: str
    health_pid: int | None
    display_healthy: bool | None
    connectors_healthy: bool | None
    health_age_seconds: float | None
    health_path: Path


@dataclass(frozen=True, slots=True)
class LaunchAgentStatus:
    loaded: bool
    pid: int | None
    state: str


def launch_agent_payload(
    *,
    repository: str | Path,
    config_path: str | Path,
    python_executable: str | Path = sys.executable,
    runtime_state_root: str | Path | None = None,
) -> dict[str, Any]:
    repository_path = Path(repository).expanduser().resolve()
    config = Path(config_path).expanduser().resolve()
    executable = Path(python_executable).expanduser().resolve()
    state = Path(runtime_state_root).expanduser().resolve() if runtime_state_root else state_root().resolve()
    app = repository_path / "apps" / "founderos.py"
    if not app.is_file():
        raise ServiceError(f"FounderOS entry point was not found: {app}")
    if not config.is_file():
        raise ServiceError(f"FounderOS configuration was not found: {config}")
    if not executable.is_file():
        raise ServiceError(f"Python executable was not found: {executable}")
    logs = state / "logs"
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(executable),
            str(app),
            "--config",
            str(config),
        ],
        "WorkingDirectory": str(repository_path),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(logs / "founderos.log"),
        "StandardErrorPath": str(logs / "founderos.error.log"),
        "EnvironmentVariables": {
            "FOUNDEROS_STATE_DIR": str(state),
            "PYTHONUNBUFFERED": "1",
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }


def emulator_launch_agent_payload(
    *,
    repository: str | Path,
    node_executable: str | Path,
    python_executable: str | Path = sys.executable,
    runtime_state_root: str | Path | None = None,
    port: int = 8080,
) -> dict[str, Any]:
    repository_path = Path(repository).expanduser().resolve()
    node = Path(node_executable).expanduser().resolve()
    python = Path(python_executable).expanduser().resolve()
    state = Path(runtime_state_root).expanduser().resolve() if runtime_state_root else state_root().resolve()
    server = repository_path / "server.js"
    web_index = repository_path / "web" / "dist" / "index.html"
    if not server.is_file():
        raise ServiceError(f"BUSY Bar emulator entry point was not found: {server}")
    if not web_index.is_file():
        raise ServiceError("BUSY Bar emulator frontend is not built; run npm run build first")
    if not node.is_file():
        raise ServiceError(f"Node.js executable was not found: {node}")
    if not python.is_file():
        raise ServiceError(f"Python executable was not found: {python}")
    if not 1 <= int(port) <= 65535:
        raise ServiceError("emulator port must be between 1 and 65535")
    logs = state / "logs"
    return {
        "Label": EMULATOR_LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(node), str(server)],
        "WorkingDirectory": str(repository_path),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(logs / "busybar-emulator.log"),
        "StandardErrorPath": str(logs / "busybar-emulator.error.log"),
        "EnvironmentVariables": {
            "BUSY_HOST": "127.0.0.1",
            "BUSY_DATA_DIR": str(state / "emulator"),
            "BUSY_PYTHON": str(python),
            "PORT": str(int(port)),
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }


def install_launch_agent(
    *,
    repository: str | Path,
    config_path: str | Path,
    python_executable: str | Path = sys.executable,
    runtime_state_root: str | Path | None = None,
) -> Path:
    _require_macos()
    state = Path(runtime_state_root).expanduser().resolve() if runtime_state_root else state_root().resolve()
    ensure_private_directory(state)
    ensure_private_directory(state / "logs")
    payload = launch_agent_payload(
        repository=repository,
        config_path=config_path,
        python_executable=python_executable,
        runtime_state_root=state,
    )
    destination = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_private_plist(destination, payload)
    domain = f"gui/{os.getuid()}"
    _run_launchctl("bootout", f"{domain}/{LAUNCH_AGENT_LABEL}", allowed_codes={0, 3, 113})
    _run_launchctl("bootstrap", domain, str(destination))
    _run_launchctl("enable", f"{domain}/{LAUNCH_AGENT_LABEL}")
    _run_launchctl("kickstart", "-k", f"{domain}/{LAUNCH_AGENT_LABEL}")
    return destination


def install_emulator_launch_agent(
    *,
    repository: str | Path,
    node_executable: str | Path,
    python_executable: str | Path = sys.executable,
    runtime_state_root: str | Path | None = None,
    port: int = 8080,
) -> Path:
    _require_macos()
    state = Path(runtime_state_root).expanduser().resolve() if runtime_state_root else state_root().resolve()
    ensure_private_directory(state)
    ensure_private_directory(state / "logs")
    ensure_private_directory(state / "emulator")
    payload = emulator_launch_agent_payload(
        repository=repository,
        node_executable=node_executable,
        python_executable=python_executable,
        runtime_state_root=state,
        port=port,
    )
    destination = Path.home() / "Library" / "LaunchAgents" / f"{EMULATOR_LAUNCH_AGENT_LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_private_plist(destination, payload)
    domain = f"gui/{os.getuid()}"
    _run_launchctl("bootout", f"{domain}/{EMULATOR_LAUNCH_AGENT_LABEL}", allowed_codes={0, 3, 113})
    _run_launchctl("bootstrap", domain, str(destination))
    _run_launchctl("enable", f"{domain}/{EMULATOR_LAUNCH_AGENT_LABEL}")
    _run_launchctl("kickstart", "-k", f"{domain}/{EMULATOR_LAUNCH_AGENT_LABEL}")
    return destination


def uninstall_launch_agent() -> bool:
    _require_macos()
    destination = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    _run_launchctl("bootout", f"{domain}/{LAUNCH_AGENT_LABEL}", allowed_codes={0, 3, 113})
    existed = destination.exists()
    if existed:
        destination.unlink()
    return existed


def uninstall_emulator_launch_agent() -> bool:
    _require_macos()
    destination = Path.home() / "Library" / "LaunchAgents" / f"{EMULATOR_LAUNCH_AGENT_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    _run_launchctl("bootout", f"{domain}/{EMULATOR_LAUNCH_AGENT_LABEL}", allowed_codes={0, 3, 113})
    existed = destination.exists()
    if existed:
        destination.unlink()
    return existed


def service_status(
    *,
    health_path: str | Path | None = None,
    stale_after_seconds: float = 90,
) -> ServiceStatus:
    health = Path(health_path).expanduser() if health_path else state_root() / "health.json"
    process = launch_agent_status(LAUNCH_AGENT_LABEL)
    health_state, age, health_pid, display_healthy, connectors_healthy = _health_status(
        health,
        stale_after_seconds=max(1.0, stale_after_seconds),
    )
    if health_state == "running" and process.pid is not None and health_pid != process.pid:
        health_state = "process_mismatch"
    return ServiceStatus(
        loaded=process.loaded,
        pid=process.pid,
        state=process.state,
        health=health_state,
        health_pid=health_pid,
        display_healthy=display_healthy,
        connectors_healthy=connectors_healthy,
        health_age_seconds=age,
        health_path=health,
    )


def launch_agent_status(label: str) -> LaunchAgentStatus:
    if sys.platform != "darwin":
        return LaunchAgentStatus(False, None, "unsupported")
    target = f"gui/{os.getuid()}/{label}"
    try:
        result = subprocess.run(
            ["/bin/launchctl", "print", target],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return LaunchAgentStatus(False, None, "unavailable")
    if result.returncode != 0:
        return LaunchAgentStatus(False, None, "not_loaded")
    pid = None
    state = "loaded"
    for line in result.stdout.splitlines():
        key, separator, value = line.strip().partition(" = ")
        if not separator:
            continue
        if key == "pid":
            try:
                pid = int(value)
            except ValueError:
                pid = None
        elif key == "state":
            state = value.strip()
    return LaunchAgentStatus(True, pid, state)


def _health_status(
    path: Path,
    stale_after_seconds: float,
) -> tuple[str, float | None, int | None, bool | None, bool | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "missing", None, None, None, None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "invalid", None, None, None, None
    generated_at = parse_datetime(payload.get("generated_at")) if isinstance(payload, Mapping) else None
    if generated_at is None:
        return "invalid", None, None, None, None
    try:
        health_pid = int(payload.get("pid"))
    except (TypeError, ValueError):
        health_pid = None
    display = payload.get("display")
    display_value = display.get("healthy") if isinstance(display, Mapping) else None
    display_healthy = display_value if isinstance(display_value, bool) else None
    connectors = payload.get("connectors")
    if isinstance(connectors, Mapping) and connectors:
        connectors_healthy = all(
            isinstance(state, Mapping) and state.get("status") == "healthy"
            for state in connectors.values()
        )
    else:
        connectors_healthy = None
    age = max(0.0, (utc_now() - generated_at.astimezone(UTC)).total_seconds())
    if age > stale_after_seconds:
        return "stale", age, health_pid, display_healthy, connectors_healthy
    return (
        str(payload.get("status") or "unknown"),
        age,
        health_pid,
        display_healthy,
        connectors_healthy,
    )


def _write_private_plist(path: Path, payload: Mapping[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            plistlib.dump(dict(payload), handle, fmt=plistlib.FMT_XML, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _run_launchctl(*arguments: str, allowed_codes: set[int] | None = None) -> None:
    try:
        result = subprocess.run(
            ["/bin/launchctl", *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"launchctl {arguments[0] if arguments else 'operation'} failed") from exc
    if result.returncode not in (allowed_codes or {0}):
        detail = " ".join((result.stderr or result.stdout).split())[:400]
        raise ServiceError(f"launchctl {' '.join(arguments[:1])} failed: {detail or result.returncode}")


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ServiceError("FounderOS LaunchAgent management is available only on macOS")
