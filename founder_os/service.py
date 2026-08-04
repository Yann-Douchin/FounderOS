"""macOS LaunchAgent installation and content-free health inspection."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from founder_os.models import UTC, parse_datetime, utc_now
from founder_os.paths import ensure_private_directory, state_root


LAUNCH_AGENT_LABEL = "com.founderos.runtime"
EMULATOR_LAUNCH_AGENT_LABEL = "com.founderos.busybar-emulator"
_RUNTIME_DIRECTORIES = ("founder_os", "apps", "public", "web/dist", "node_modules")
_RUNTIME_FILES = ("server.js", "screen_renderer.js", "package.json", "package-lock.json")
_IGNORED_RUNTIME_DIRECTORIES = {"__pycache__", ".bin"}
_IGNORED_RUNTIME_FILES = {".DS_Store"}
_IGNORED_RUNTIME_SUFFIXES = {".pyc", ".pyo"}
_IGNORED_RUNTIME_SUBTREES: set[Path] = set()
_LAUNCHCTL_BOOTSTRAP_ATTEMPTS = 7
_LAUNCHCTL_BOOTSTRAP_DELAY_SECONDS = 0.5
_LAUNCHCTL_BOOTSTRAP_DELAY_MAX_SECONDS = 4.0
_MINIMUM_NODE_VERSION = (20, 9, 0)


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
    automations_healthy: bool | None
    health_age_seconds: float | None
    health_path: Path


@dataclass(frozen=True, slots=True)
class LaunchAgentStatus:
    loaded: bool
    pid: int | None
    state: str


@dataclass(frozen=True, slots=True)
class RuntimeDeployment:
    root: Path
    config_path: Path
    deployment_id: str


@dataclass(frozen=True, slots=True)
class LaunchAgentSnapshot:
    label: str
    destination: Path
    plist_data: bytes | None
    loaded: bool


def stage_runtime_bundle(
    *,
    repository: str | Path,
    config_path: str | Path,
    runtime_state_root: str | Path | None = None,
) -> RuntimeDeployment:
    """Create an immutable private copy that launchd can read outside protected folders."""
    repository_path = Path(repository).expanduser().resolve()
    config = Path(config_path).expanduser().resolve()
    state = Path(runtime_state_root).expanduser().resolve() if runtime_state_root else state_root().resolve()
    sources = _runtime_sources(repository_path)
    _require_regular_file(config, "FounderOS configuration")
    ensure_private_directory(state)
    deployments = ensure_private_directory(state / "deployments")
    staged_config = Path("founderos.runtime.json")
    deployment_id = _runtime_digest([*sources, (staged_config, config)])
    destination = deployments / deployment_id
    if destination.exists():
        _validate_runtime_deployment(destination, deployment_id)
        return RuntimeDeployment(destination, destination / staged_config, deployment_id)
    temporary = Path(tempfile.mkdtemp(prefix=".staging-", dir=deployments))
    temporary.chmod(0o700)
    digest = hashlib.sha256()
    try:
        for relative, source in sources:
            _copy_private_file(
                source,
                temporary / relative,
                root=temporary,
                digest=digest,
                logical_path=relative,
            )
        _copy_private_file(
            config,
            temporary / staged_config,
            root=temporary,
            digest=digest,
            logical_path=staged_config,
        )
        if digest.hexdigest() != deployment_id:
            raise ServiceError("runtime source changed between validation and staging")
        metadata = json.dumps(
            {
                "schema": 1,
                "deployment_id": deployment_id,
                "file_count": len(sources) + 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        _write_private_bytes(temporary / ".deployment.json", metadata)
        _fsync_directory(temporary)
        try:
            os.rename(temporary, destination)
            _fsync_directory(deployments)
        except OSError:
            if not destination.exists():
                raise
            _validate_runtime_deployment(destination, deployment_id)
        return RuntimeDeployment(
            root=destination,
            config_path=destination / staged_config,
            deployment_id=deployment_id,
        )
    except ServiceError:
        raise
    except (OSError, ValueError) as exc:
        raise ServiceError("FounderOS runtime deployment could not be staged") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


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
    renderer = repository_path / "screen_renderer.js"
    sharp_manifest = repository_path / "node_modules" / "sharp" / "package.json"
    web_index = repository_path / "web" / "dist" / "index.html"
    if not server.is_file():
        raise ServiceError(f"BUSY Bar emulator entry point was not found: {server}")
    if not web_index.is_file():
        raise ServiceError("BUSY Bar emulator frontend is not built; run npm run build first")
    if not renderer.is_file() or not sharp_manifest.is_file():
        raise ServiceError("BUSY Bar screen decoder is not installed; run npm ci first")
    if not node.is_file():
        raise ServiceError(f"Node.js executable was not found: {node}")
    _validate_node_version(node)
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
            "FOUNDEROS_CLOSURE_SNAPSHOT": str(state / "obligations.json"),
            "PORT": str(int(port)),
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }


def _validate_node_version(executable: Path) -> tuple[int, int, int]:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceError(f"Node.js version could not be inspected: {executable}") from exc
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
    if result.returncode != 0 or not match:
        raise ServiceError(f"Node.js returned an invalid version: {executable}")
    version = tuple(int(part) for part in match.groups())
    if version < _MINIMUM_NODE_VERSION:
        required = ".".join(str(part) for part in _MINIMUM_NODE_VERSION)
        found = ".".join(str(part) for part in version)
        raise ServiceError(f"Node.js {required} or newer is required, found {found}")
    return version


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
    return _replace_launch_agent(LAUNCH_AGENT_LABEL, payload)


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
    return _replace_launch_agent(EMULATOR_LAUNCH_AGENT_LABEL, payload)


def capture_launch_agent(label: str) -> LaunchAgentSnapshot:
    """Capture the durable definition and loaded state before a supervised update."""
    _require_macos()
    destination = _launch_agent_destination(label)
    try:
        plist_data = destination.read_bytes()
    except FileNotFoundError:
        plist_data = None
    except OSError as exc:
        raise ServiceError(f"existing LaunchAgent definition could not be read: {label}") from exc
    if plist_data is not None and len(plist_data) > 1024 * 1024:
        raise ServiceError(f"existing LaunchAgent definition is unexpectedly large: {label}")
    return LaunchAgentSnapshot(
        label=label,
        destination=destination,
        plist_data=plist_data,
        loaded=launch_agent_status(label).loaded,
    )


def restore_launch_agent(snapshot: LaunchAgentSnapshot) -> None:
    """Restore the exact prior plist and loaded state after a failed readiness check."""
    _require_macos()
    destination = _launch_agent_destination(snapshot.label)
    if destination != snapshot.destination:
        raise ServiceError("LaunchAgent snapshot destination does not match its label")
    domain = f"gui/{os.getuid()}"
    _run_launchctl("bootout", f"{domain}/{snapshot.label}", allowed_codes={0, 3, 113})
    if snapshot.plist_data is None:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ServiceError(f"failed LaunchAgent definition could not be removed: {snapshot.label}") from exc
        if snapshot.loaded:
            raise ServiceError(
                f"previous loaded LaunchAgent had no persistent definition and cannot be restored: {snapshot.label}"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_private_bytes(destination, snapshot.plist_data)
    if snapshot.loaded:
        _bootstrap_launch_agent(domain, snapshot.label, destination)
        _run_launchctl("enable", f"{domain}/{snapshot.label}")
        _run_launchctl("kickstart", f"{domain}/{snapshot.label}")


def uninstall_launch_agent() -> bool:
    _require_macos()
    destination = _launch_agent_destination(LAUNCH_AGENT_LABEL)
    domain = f"gui/{os.getuid()}"
    _run_launchctl("bootout", f"{domain}/{LAUNCH_AGENT_LABEL}", allowed_codes={0, 3, 113})
    existed = destination.exists()
    if existed:
        destination.unlink()
    return existed


def uninstall_emulator_launch_agent() -> bool:
    _require_macos()
    destination = _launch_agent_destination(EMULATOR_LAUNCH_AGENT_LABEL)
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
    health_state, age, health_pid, display_healthy, connectors_healthy, automations_healthy = _health_status(
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
        automations_healthy=automations_healthy,
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
) -> tuple[str, float | None, int | None, bool | None, bool | None, bool | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "missing", None, None, None, None, None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "invalid", None, None, None, None, None
    generated_at = parse_datetime(payload.get("generated_at")) if isinstance(payload, Mapping) else None
    if generated_at is None:
        return "invalid", None, None, None, None, None
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
            _connector_heartbeat_is_ready(state)
            for state in connectors.values()
        )
    else:
        connectors_healthy = None
    automations = payload.get("automations")
    if isinstance(automations, Mapping):
        automations_healthy = all(
            isinstance(state, Mapping) and state.get("status") == "healthy"
            for state in automations.values()
        )
    else:
        automations_healthy = None
    age = max(0.0, (utc_now() - generated_at.astimezone(UTC)).total_seconds())
    if age > stale_after_seconds:
        return "stale", age, health_pid, display_healthy, connectors_healthy, automations_healthy
    return (
        str(payload.get("status") or "unknown"),
        age,
        health_pid,
        display_healthy,
        connectors_healthy,
        automations_healthy,
    )


def _connector_heartbeat_is_ready(state: Any) -> bool:
    if not isinstance(state, Mapping):
        return False
    status = str(state.get("status") or "")
    if status == "healthy":
        return True
    failures = state.get("failures")
    return bool(
        status == "polling"
        and isinstance(state.get("last_success_at"), str)
        and str(state.get("last_success_at")).strip()
        and isinstance(failures, int)
        and not isinstance(failures, bool)
        and failures == 0
        and state.get("error_present") is False
    )


def _runtime_sources(repository: Path) -> list[tuple[Path, Path]]:
    if repository.is_symlink() or not repository.is_dir():
        raise ServiceError(f"FounderOS repository was not found: {repository}")
    sources: list[tuple[Path, Path]] = []
    for relative_name in _RUNTIME_DIRECTORIES:
        relative_root = Path(relative_name)
        source_root = repository / relative_root
        if source_root.is_symlink() or not source_root.is_dir():
            raise ServiceError(f"required runtime directory was not found: {relative_name}")
        for current_name, directory_names, file_names in os.walk(source_root, followlinks=False):
            current = Path(current_name)
            retained_directories: list[str] = []
            for name in sorted(directory_names):
                candidate = current / name
                candidate_relative = relative_root / candidate.relative_to(source_root)
                mode = candidate.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise ServiceError(f"runtime source contains a symbolic link: {candidate}")
                if not stat.S_ISDIR(mode):
                    raise ServiceError(f"runtime source contains a non-directory entry: {candidate}")
                if (
                    name not in _IGNORED_RUNTIME_DIRECTORIES
                    and candidate_relative not in _IGNORED_RUNTIME_SUBTREES
                ):
                    retained_directories.append(name)
            directory_names[:] = retained_directories
            for name in sorted(file_names):
                source = current / name
                mode = source.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise ServiceError(f"runtime source contains a symbolic link: {source}")
                if name in _IGNORED_RUNTIME_FILES or source.suffix in _IGNORED_RUNTIME_SUFFIXES:
                    continue
                if not stat.S_ISREG(mode):
                    raise ServiceError(f"runtime source contains a non-regular file: {source}")
                nested = current.relative_to(source_root) / name
                sources.append((relative_root / nested, source))
    for relative_name in _RUNTIME_FILES:
        relative = Path(relative_name)
        source = repository / relative
        _require_regular_file(source, f"required runtime file {relative_name}")
        sources.append((relative, source))
    if not (repository / "apps" / "founderos.py").is_file():
        raise ServiceError("FounderOS runtime entry point is missing")
    if not (repository / "web" / "dist" / "index.html").is_file():
        raise ServiceError("BUSY Bar emulator frontend is not built; run npm run build first")
    return sorted(sources, key=lambda item: item[0].as_posix())


def _require_regular_file(path: Path, description: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ServiceError(f"{description} was not found: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ServiceError(f"{description} must be a regular file: {path}")


def _copy_private_file(
    source: Path,
    destination: Path,
    *,
    root: Path,
    digest: Any,
    logical_path: Path,
) -> None:
    if destination != root / logical_path:
        raise ServiceError("runtime deployment destination escaped its staging root")
    current = root
    for part in logical_path.parts[:-1]:
        current = ensure_private_directory(current / part)
    content = _read_stable_file(source)
    digest.update(logical_path.as_posix().encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(len(content)).encode("ascii"))
    digest.update(b"\0")
    digest.update(content)
    digest.update(b"\0")
    _write_staged_file(destination, content)


def _runtime_digest(sources: list[tuple[Path, Path]]) -> str:
    digest = hashlib.sha256()
    for logical_path, source in sources:
        content = _read_stable_file(source)
        digest.update(logical_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _write_staged_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    path.chmod(0o600)


def _read_stable_file(source: Path) -> bytes:
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    previous_content: bytes | None = None
    for _ in range(4):
        source_descriptor = os.open(source, source_flags)
        try:
            before = os.fstat(source_descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ServiceError(f"runtime source must be a regular file: {source}")
            chunks: list[bytes] = []
            copied = 0
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                copied += len(chunk)
            after = os.fstat(source_descriptor)
            content = b"".join(chunks)
            metadata_stable = (
                copied == before.st_size
                and after.st_size == before.st_size
                and after.st_mtime_ns == before.st_mtime_ns
                and after.st_ino == before.st_ino
            )
            if metadata_stable or content == previous_content:
                return content
            previous_content = content
        finally:
            os.close(source_descriptor)
    raise ServiceError(f"runtime source changed while it was being staged: {source}")


def _validate_runtime_deployment(root: Path, deployment_id: str) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ServiceError(f"runtime deployment is not a private directory: {root}")
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise ServiceError(f"runtime deployment permissions are not private: {root}")
    marker = root / ".deployment.json"
    _require_regular_file(marker, "runtime deployment marker")
    try:
        metadata = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError(f"runtime deployment marker is invalid: {root}") from exc
    if not isinstance(metadata, Mapping) or metadata.get("deployment_id") != deployment_id:
        raise ServiceError(f"runtime deployment marker does not match its directory: {root}")
    for required in (
        "apps/founderos.py",
        "founder_os/__init__.py",
        "founderos.runtime.json",
        "server.js",
        "screen_renderer.js",
        "node_modules/sharp/package.json",
        "web/dist/index.html",
    ):
        path = root / required
        _require_regular_file(path, f"staged runtime file {required}")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ServiceError(f"staged runtime file permissions are not private: {path}")
    actual_id = _runtime_digest([
        *_runtime_sources(root),
        (Path("founderos.runtime.json"), root / "founderos.runtime.json"),
    ])
    if actual_id != deployment_id:
        raise ServiceError(f"runtime deployment content does not match its identifier: {root}")


def _launch_agent_destination(label: str) -> Path:
    if label not in {LAUNCH_AGENT_LABEL, EMULATOR_LAUNCH_AGENT_LABEL}:
        raise ServiceError(f"unsupported LaunchAgent label: {label}")
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _replace_launch_agent(label: str, payload: Mapping[str, Any]) -> Path:
    snapshot = capture_launch_agent(label)
    destination = snapshot.destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    try:
        _write_private_plist(destination, payload)
        _run_launchctl("bootout", f"{domain}/{label}", allowed_codes={0, 3, 113})
        _bootstrap_launch_agent(domain, label, destination)
        _run_launchctl("enable", f"{domain}/{label}")
        _run_launchctl("kickstart", f"{domain}/{label}")
    except (OSError, ServiceError) as exc:
        try:
            restore_launch_agent(snapshot)
        except ServiceError as rollback_error:
            raise ServiceError(
                f"LaunchAgent update failed and its previous definition could not be restored: {label}"
            ) from rollback_error
        if isinstance(exc, ServiceError):
            raise
        raise ServiceError(f"LaunchAgent definition could not be installed: {label}") from exc
    return destination


def _write_private_plist(path: Path, payload: Mapping[str, Any]) -> None:
    _write_private_bytes(
        path,
        plistlib.dumps(dict(payload), fmt=plistlib.FMT_XML, sort_keys=True),
    )


def _write_private_bytes(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _bootstrap_launch_agent(domain: str, label: str, destination: Path) -> None:
    latest_error: ServiceError | None = None
    for attempt in range(_LAUNCHCTL_BOOTSTRAP_ATTEMPTS):
        try:
            _run_launchctl("bootstrap", domain, str(destination))
            return
        except ServiceError as exc:
            latest_error = exc
            if attempt + 1 >= _LAUNCHCTL_BOOTSTRAP_ATTEMPTS:
                break
            _run_launchctl("bootout", f"{domain}/{label}", allowed_codes={0, 3, 113})
            time.sleep(
                min(
                    _LAUNCHCTL_BOOTSTRAP_DELAY_MAX_SECONDS,
                    _LAUNCHCTL_BOOTSTRAP_DELAY_SECONDS * (2 ** attempt),
                )
            )
    if latest_error is not None:
        raise ServiceError(f"{label}: {latest_error}") from latest_error
    raise ServiceError(f"launchctl bootstrap did not run: {label}")


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
