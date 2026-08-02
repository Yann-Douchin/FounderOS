#!/usr/bin/env python3
"""Provision credentials and operate the autonomous FounderOS service."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]


def _delegate_protected_service_install() -> None:
    if __name__ != "__main__" or os.environ.get("FOUNDEROS_BOOTSTRAPPED") == "1":
        return
    arguments = sys.argv[1:]
    if not any(
        arguments[index : index + 2] == ["service", "install"]
        for index in range(max(0, len(arguments) - 1))
    ):
        return
    bootstrap = REPO_ROOT / "apps" / "founderos_install.zsh"
    os.execv("/bin/zsh", ["/bin/zsh", str(bootstrap), *arguments])


_delegate_protected_service_install()


if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from founder_os.config import load_config  # noqa: E402
from founder_os.display.busybar import BusyBarDisplay, DisplayError  # noqa: E402
from founder_os.oauth import OAuthFlowError, authorize_google, authorize_linear  # noqa: E402
from founder_os.paths import state_root  # noqa: E402
from founder_os.secrets import (  # noqa: E402
    SecretError,
    build_secret_resolver,
    keychain_store_from_config,
)
from founder_os.service import (  # noqa: E402
    EMULATOR_LAUNCH_AGENT_LABEL,
    LAUNCH_AGENT_LABEL,
    LaunchAgentSnapshot,
    ServiceError,
    capture_launch_agent,
    install_emulator_launch_agent,
    install_launch_agent,
    launch_agent_status,
    restore_launch_agent,
    service_status,
    stage_runtime_bundle,
    uninstall_emulator_launch_agent,
    uninstall_launch_agent,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="founderos.local.json", help="FounderOS configuration path")
    commands = parser.add_subparsers(dest="command", required=True)

    secret = commands.add_parser("secret", help="Manage macOS Keychain entries")
    secret_commands = secret.add_subparsers(dest="secret_command", required=True)
    secret_commands.add_parser("status", help="Show configured or missing entries without values")
    secret_set = secret_commands.add_parser("set", help="Read one value securely and store it")
    secret_set.add_argument("account", help="Environment-shaped secret account name")
    secret_clipboard = secret_commands.add_parser(
        "import-clipboard",
        help="Import one value from the macOS clipboard, then clear the clipboard",
    )
    secret_clipboard.add_argument("account", help="Environment-shaped secret account name")
    secret_delete = secret_commands.add_parser("delete", help="Delete one Keychain entry")
    secret_delete.add_argument("account")

    auth = commands.add_parser("auth", help="Run a least-privilege OAuth flow")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    google = auth_commands.add_parser("google", help="Authorize read-only Gmail and Calendar")
    google.add_argument("--client-json", required=True, help="Downloaded Google OAuth client JSON")
    google.add_argument("--timeout", type=float, default=240)
    google.add_argument("--manual", action="store_true", help="Print the URL instead of opening a browser")
    linear = auth_commands.add_parser("linear", help="Authorize read-only Linear through PKCE")
    linear.add_argument("--client-id", required=True)
    linear.add_argument("--callback-port", type=int, default=8766)
    linear.add_argument("--timeout", type=float, default=240)
    linear.add_argument("--manual", action="store_true", help="Print the URL instead of opening a browser")

    service = commands.add_parser("service", help="Manage the macOS LaunchAgent")
    service_commands = service.add_subparsers(dest="service_command", required=True)
    install = service_commands.add_parser("install", help="Preflight and install the LaunchAgent")
    install.add_argument("--skip-emulator", action="store_true")
    service_commands.add_parser("status", help="Show launchd and heartbeat state")
    service_commands.add_parser("uninstall", help="Unload the service and remove its plist")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config_path = Path(args.config).expanduser().resolve()
        config = load_config(config_path)
        if args.command == "secret":
            return _secret_command(args, config)
        if args.command == "auth":
            return _auth_command(args, config)
        if args.command == "service":
            return _service_command(args, config, config_path)
    except (OAuthFlowError, SecretError, ServiceError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def _secret_command(args: argparse.Namespace, config: dict) -> int:
    accounts = [str(value) for value in config["secrets"].get("accounts", [])]
    if args.secret_command == "status":
        resolver = build_secret_resolver(config["secrets"])
        states = {account: bool(resolver.get(account)) for account in accounts}
        for account in accounts:
            print(f"{account}: {'configured' if states[account] else 'missing'}")
        return 0 if accounts and all(states.values()) else 3
    store = keychain_store_from_config(config["secrets"])
    account = str(args.account).strip()
    if account not in accounts:
        raise SecretError(f"secret account is not allowlisted by configuration: {account}")
    if args.secret_command == "set":
        value = getpass.getpass(f"{account}: ")
        if not value:
            raise SecretError("secret value cannot be empty")
        store.set(account, value)
        print(f"{account}: configured")
        return 0
    if args.secret_command == "import-clipboard":
        value = _read_clipboard_secret()
        try:
            store.set(account, value)
        finally:
            _clear_clipboard()
        print(f"{account}: configured; clipboard cleared")
        return 0
    removed = store.delete(account)
    print(f"{account}: {'deleted' if removed else 'not found'}")
    return 0


def _auth_command(args: argparse.Namespace, config: dict) -> int:
    store = keychain_store_from_config(config["secrets"])
    if args.auth_command == "google":
        options = {"browser_opener": _manual_browser} if args.manual else {}
        result = authorize_google(
            args.client_json,
            store,
            timeout_seconds=args.timeout,
            **options,
        )
    else:
        options = {"browser_opener": _manual_browser} if args.manual else {}
        result = authorize_linear(
            args.client_id,
            store,
            callback_port=args.callback_port,
            timeout_seconds=args.timeout,
            **options,
        )
    print(f"{result.provider}: authorized")
    for account in result.accounts:
        print(f"{account}: configured")
    return 0


def _service_command(args: argparse.Namespace, config: dict, config_path: Path) -> int:
    health_path = Path(str(config["operations"]["health_path"]))
    if args.service_command == "status":
        status = service_status(
            health_path=health_path,
            stale_after_seconds=max(90.0, float(config["operations"]["heartbeat_seconds"]) * 4),
        )
        emulator = launch_agent_status(EMULATOR_LAUNCH_AGENT_LABEL)
        emulator_required = _uses_loopback_display(config)
        print(json.dumps({
            "loaded": status.loaded,
            "pid": status.pid,
            "state": status.state,
            "health": status.health,
            "health_pid": status.health_pid,
            "display_healthy": status.display_healthy,
            "connectors_healthy": status.connectors_healthy,
            "health_age_seconds": status.health_age_seconds,
            "health_path": str(status.health_path),
            "emulator": {
                "required": emulator_required,
                "loaded": emulator.loaded,
                "pid": emulator.pid,
                "state": emulator.state,
            },
        }, ensure_ascii=False, indent=2))
        emulator_ready = (emulator.loaded and emulator.pid is not None) or not emulator_required
        return 0 if (
            status.loaded
            and status.health == "running"
            and status.display_healthy is True
            and status.connectors_healthy is True
            and emulator_ready
        ) else 3
    if args.service_command == "uninstall":
        runtime_removed = uninstall_launch_agent()
        emulator_removed = uninstall_emulator_launch_agent()
        print("FounderOS LaunchAgent removed" if runtime_removed else "FounderOS LaunchAgent was not installed")
        print(
            "BUSY Bar emulator LaunchAgent removed"
            if emulator_removed
            else "BUSY Bar emulator LaunchAgent was not installed"
        )
        return 0
    _preflight(config_path)
    state = state_root()
    deployment = stage_runtime_bundle(
        repository=REPO_ROOT,
        config_path=config_path,
        runtime_state_root=state,
    )
    emulator_snapshot = (
        capture_launch_agent(EMULATOR_LAUNCH_AGENT_LABEL)
        if not args.skip_emulator
        else None
    )
    runtime_snapshot = capture_launch_agent(LAUNCH_AGENT_LABEL)
    emulator_changed = False
    runtime_changed = False
    emulator_destination = None
    try:
        if not args.skip_emulator:
            node_executable = shutil.which("node")
            if not node_executable:
                raise ServiceError("Node.js is required to supervise the BUSY Bar emulator")
            emulator_destination = install_emulator_launch_agent(
                repository=deployment.root,
                node_executable=node_executable,
                python_executable=sys.executable,
                runtime_state_root=state,
                port=_emulator_port(config),
            )
            emulator_changed = True
            _wait_for_emulator(config, timeout_seconds=45.0)
        else:
            _validate_display(config, wait_seconds=0.0)
        destination = install_launch_agent(
            repository=deployment.root,
            config_path=deployment.config_path,
            python_executable=sys.executable,
            runtime_state_root=state,
        )
        runtime_changed = True
        _wait_for_runtime(config, timeout_seconds=90.0)
    except ServiceError as exc:
        snapshots: list[LaunchAgentSnapshot] = []
        if runtime_changed:
            snapshots.append(runtime_snapshot)
        if emulator_changed and emulator_snapshot is not None:
            snapshots.append(emulator_snapshot)
        rollback_failures = _restore_launch_agents(snapshots)
        if rollback_failures:
            labels = ", ".join(rollback_failures)
            raise ServiceError(f"service update failed and rollback was incomplete: {labels}") from exc
        raise
    if emulator_destination is not None:
        print(f"BUSY Bar emulator LaunchAgent installed: {emulator_destination}")
    print(f"FounderOS LaunchAgent installed: {destination}")
    print(f"FounderOS runtime deployed: {deployment.root}")
    return 0


def _restore_launch_agents(snapshots: list[LaunchAgentSnapshot]) -> list[str]:
    failures: list[str] = []
    for snapshot in snapshots:
        try:
            restore_launch_agent(snapshot)
        except ServiceError:
            failures.append(snapshot.label)
    return failures


def _wait_for_runtime(config: dict, *, timeout_seconds: float) -> None:
    health_path = Path(str(config["operations"]["health_path"]))
    stale_after = max(90.0, float(config["operations"]["heartbeat_seconds"]) * 4)
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    latest = None
    while time.monotonic() < deadline:
        latest = service_status(
            health_path=health_path,
            stale_after_seconds=stale_after,
        )
        if (
            latest.loaded
            and latest.health == "running"
            and latest.display_healthy is True
            and latest.connectors_healthy is True
        ):
            return
        time.sleep(0.25)
    if latest is None or not latest.loaded:
        detail = "not_loaded"
    elif latest.health != "running":
        detail = latest.health
    elif latest.display_healthy is not True:
        detail = "display_unhealthy"
    else:
        detail = "connectors_unhealthy"
    raise ServiceError(f"FounderOS LaunchAgent did not become healthy: {detail}")


def _emulator_port(config: dict) -> int:
    raw_host = str(config["display"]["host"]).strip()
    parsed = urlsplit(raw_host if "://" in raw_host else f"//{raw_host}")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ServiceError(
            "automatic emulator supervision requires a loopback display host; use --skip-emulator for hardware"
        )
    try:
        return int(parsed.port or 8080)
    except ValueError as exc:
        raise ServiceError("display host contains an invalid emulator port") from exc


def _uses_loopback_display(config: dict) -> bool:
    raw_host = str(config["display"]["host"]).strip()
    parsed = urlsplit(raw_host if "://" in raw_host else f"//{raw_host}")
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _validate_display(config: dict, *, wait_seconds: float) -> None:
    display_config = config["display"]
    token_account = str(display_config.get("api_token_env", "")).strip()
    resolver = build_secret_resolver(config["secrets"])
    display = BusyBarDisplay(
        str(display_config["host"]),
        application_name=str(display_config["application_name"]),
        priority=int(display_config["device_priority"]),
        timeout=float(display_config["request_timeout_seconds"]),
        api_token=resolver.get(token_account) if token_account else "",
        api_semver=str(display_config["api_semver"]),
    )
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        try:
            actual = display.version()
            break
        except DisplayError as exc:
            if time.monotonic() >= deadline:
                raise ServiceError(f"BUSY Bar display validation failed: {exc}") from exc
            time.sleep(0.25)
    expected = str(display_config["api_semver"])
    if actual.split(".", 1)[0] != expected.split(".", 1)[0]:
        raise ServiceError(f"BUSY Bar API major {actual!r} is incompatible with expected {expected!r}")


def _wait_for_emulator(config: dict, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    latest_state = "not_loaded"
    while time.monotonic() < deadline:
        process = launch_agent_status(EMULATOR_LAUNCH_AGENT_LABEL)
        latest_state = process.state
        if process.loaded and process.pid is not None:
            try:
                _validate_display(config, wait_seconds=0.0)
                confirmed = launch_agent_status(EMULATOR_LAUNCH_AGENT_LABEL)
                if confirmed.pid == process.pid and confirmed.loaded:
                    return
                latest_state = "process_restarted"
            except ServiceError:
                latest_state = "display_unavailable"
        time.sleep(0.25)
    raise ServiceError(f"BUSY Bar emulator LaunchAgent did not become healthy: {latest_state}")


def _preflight(config_path: Path) -> None:
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "apps" / "founderos.py"),
                "--config",
                str(config_path),
                "--once",
                "--dry-run",
                "--require-healthy",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError("live connector preflight could not complete") from exc
    if result.returncode != 0:
        raise ServiceError(f"live connector preflight failed with exit code {result.returncode}")


def _manual_browser(url: str) -> bool:
    print(f"Authorization URL: {url}", flush=True)
    return True


def _read_clipboard_secret() -> str:
    try:
        result = subprocess.run(
            ["/usr/bin/pbpaste"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretError("could not read the macOS clipboard") from exc
    if result.returncode != 0:
        raise SecretError("could not read the macOS clipboard")
    value = result.stdout.strip()
    if not value:
        raise SecretError("clipboard does not contain a secret value")
    return value


def _clear_clipboard() -> None:
    try:
        result = subprocess.run(
            ["/usr/bin/pbcopy"],
            input="",
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretError("secret was stored, but the macOS clipboard could not be cleared") from exc
    if result.returncode != 0:
        raise SecretError("secret was stored, but the macOS clipboard could not be cleared")


if __name__ == "__main__":
    raise SystemExit(main())
