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
from dataclasses import replace
from datetime import datetime
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
from founder_os.closure import Evidence, Gate, ObligationLedger, Relationship  # noqa: E402
from founder_os.closure.ledger import LedgerError  # noqa: E402
from founder_os.display.busybar import BusyBarDisplay, DisplayError  # noqa: E402
from founder_os.display.verification import verify_french_glyphs  # noqa: E402
from founder_os.oauth import (  # noqa: E402
    GOOGLE_DRIVE_SCOPE,
    GOOGLE_SHEETS_SCOPE,
    OAuthFlowError,
    authorize_google,
    authorize_linear,
    google_scopes_for_connectors,
)
from founder_os.models import parse_datetime, utc_now  # noqa: E402
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
    google = auth_commands.add_parser("google", help="Authorize enabled read-only Google connectors")
    google.add_argument("--client-json", required=True, help="Downloaded Google OAuth client JSON")
    google.add_argument("--timeout", type=float, default=240)
    google.add_argument("--manual", action="store_true", help="Print the URL instead of opening a browser")
    google.add_argument("--include-drive", action="store_true", help="Also request read-only Drive metadata")
    google.add_argument("--include-sheets", action="store_true", help="Also request read-only Sheets values")
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

    display = commands.add_parser("display", help="Inspect and verify the BUSY Bar display")
    display_commands = display.add_subparsers(dest="display_command", required=True)
    display_commands.add_parser("status", help="Show firmware capabilities and screen readback sizes")
    verify = display_commands.add_parser("verify-accents", help="Verify French glyphs using screen readback")
    verify.add_argument(
        "--raster-fallback",
        action="store_true",
        help="Verify the PNG raster fallback instead of the native global font",
    )

    obligation = commands.add_parser("obligation", help="Inspect and correct the commitment ledger")
    obligation_commands = obligation.add_subparsers(dest="obligation_command", required=True)
    obligation_list = obligation_commands.add_parser("list", help="List active obligations")
    obligation_list.add_argument("--all", action="store_true", help="Include closed and cancelled obligations")
    obligation_list.add_argument("--limit", type=int, default=100)
    obligation_show = obligation_commands.add_parser("show", help="Show one obligation and its audit")
    obligation_show.add_argument("id")
    obligation_state = obligation_commands.add_parser("state", help="Correct an obligation state")
    obligation_state.add_argument("id")
    obligation_state.add_argument("state", choices=["open", "waiting", "blocked", "ready", "deferred", "closed", "cancelled"])
    obligation_state.add_argument("--reason", required=True)
    obligation_assign = obligation_commands.add_parser("assign", help="Correct the current owner")
    obligation_assign.add_argument("id")
    obligation_assign.add_argument("owner")
    obligation_next = obligation_commands.add_parser("next", help="Correct the next action holder")
    obligation_next.add_argument("id")
    obligation_next.add_argument("actor")
    obligation_delegate = obligation_commands.add_parser("delegate", help="Record or clear a capacity delegate")
    obligation_delegate.add_argument("id")
    obligation_delegate.add_argument("actor", help="Delegate name or none")
    obligation_action = obligation_commands.add_parser("action", help="Record the next concrete action")
    obligation_action.add_argument("id")
    obligation_action.add_argument("text")
    obligation_action.add_argument("--actor", default="")
    obligation_due = obligation_commands.add_parser("due", help="Correct or clear the due date")
    obligation_due.add_argument("id")
    obligation_due.add_argument("value", help="ISO-8601 timestamp or none")
    obligation_gate = obligation_commands.add_parser("gate", help="Correct one operational gate")
    obligation_gate.add_argument("id")
    obligation_gate.add_argument("name")
    obligation_gate.add_argument("state", choices=["pending", "blocked", "satisfied", "waived"])
    obligation_gate.add_argument("--owner", default="")
    obligation_gate.add_argument("--detail", default="")
    obligation_evidence = obligation_commands.add_parser("evidence", help="Attach operator-verified evidence")
    obligation_evidence.add_argument("id")
    obligation_evidence.add_argument("category")
    obligation_evidence.add_argument("--scope", default="")
    obligation_evidence.add_argument("--detail", default="")
    obligation_evidence.add_argument("--expires-at", default="")

    relationship = commands.add_parser("relationship", help="Inspect and correct relationship memory")
    relationship_commands = relationship.add_subparsers(dest="relationship_command", required=True)
    relationship_list = relationship_commands.add_parser("list", help="List relationship records")
    relationship_list.add_argument("--limit", type=int, default=100)
    relationship_show = relationship_commands.add_parser("show", help="Show one relationship and its audit")
    relationship_show.add_argument("key")
    relationship_set = relationship_commands.add_parser("set", help="Set stage and follow-up boundaries")
    relationship_set.add_argument("key")
    relationship_set.add_argument("--name")
    relationship_set.add_argument("--stage")
    relationship_set.add_argument("--next-decision")
    relationship_set.add_argument("--resume-after")
    relationship_set.add_argument("--cooling-off-until")
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
        if args.command == "display":
            return _display_command(args, config)
        if args.command == "obligation":
            return _obligation_command(args, config)
        if args.command == "relationship":
            return _relationship_command(args, config)
    except (DisplayError, LedgerError, OAuthFlowError, SecretError, ServiceError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def _obligation_command(args: argparse.Namespace, config: dict) -> int:
    ledger = _obligation_ledger(config)
    try:
        if args.obligation_command == "list":
            obligations = ledger.list(active_only=not args.all, limit=args.limit)
            print(json.dumps([_obligation_summary(value) for value in obligations], ensure_ascii=False, indent=2))
            return 0
        obligation = ledger.get(str(args.id))
        if obligation is None:
            raise LedgerError(f"unknown obligation: {args.id}")
        if args.obligation_command == "show":
            payload = obligation.to_dict()
            payload["transitions"] = ledger.transitions(obligation.id)
            payload["audit"] = ledger.audit(obligation.id)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.obligation_command == "state":
            changed = ledger.transition(obligation.id, args.state, reason=args.reason)
        elif args.obligation_command == "assign":
            changed = ledger.correct(obligation.id, {"owner": args.owner}, actor="founderosctl")
        elif args.obligation_command == "next":
            changed = ledger.correct(obligation.id, {"next_actor": args.actor}, actor="founderosctl")
        elif args.obligation_command == "delegate":
            changed = ledger.correct_metadata(
                obligation.id,
                {"delegate": "" if args.actor.casefold() == "none" else args.actor},
                actor="founderosctl",
            )
        elif args.obligation_command == "action":
            changed = _record_action(ledger, obligation, args)
        elif args.obligation_command == "due":
            changed = ledger.correct(
                obligation.id,
                {"due_at": None if args.value.casefold() == "none" else _required_datetime(args.value).isoformat()},
                actor="founderosctl",
            )
        elif args.obligation_command == "gate":
            changed = _correct_gate(ledger, obligation, args)
        else:
            changed = _attach_evidence(ledger, obligation, args)
        print(json.dumps(_obligation_summary(changed), ensure_ascii=False, indent=2))
        return 0
    finally:
        ledger.close()


def _relationship_command(args: argparse.Namespace, config: dict) -> int:
    ledger = _obligation_ledger(config)
    try:
        if args.relationship_command == "list":
            print(json.dumps(
                [value.to_dict() for value in ledger.relationships(limit=args.limit)],
                ensure_ascii=False,
                indent=2,
            ))
            return 0
        current = ledger.relationship(args.key)
        if args.relationship_command == "show":
            if current is None:
                raise LedgerError(f"unknown relationship: {args.key}")
            payload = current.to_dict()
            payload["audit"] = ledger.audit(current.key)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        metadata = dict(current.metadata) if current else {}
        manual_fields = {
            str(value) for value in metadata.get("manual_fields", [])
        } if isinstance(metadata.get("manual_fields", []), list) else set()
        supplied = {
            name
            for name in ("name", "stage", "next_decision", "resume_after", "cooling_off_until")
            if getattr(args, name) is not None
        }
        if not supplied:
            raise ValueError("relationship set requires at least one field")
        manual_fields.update(supplied)
        metadata["manual_fields"] = sorted(manual_fields)
        metadata["manual_correction"] = {
            "actor": "founderosctl",
            "at": utc_now().isoformat(),
            "fields": sorted(supplied),
        }
        next_decision = (
            ""
            if args.next_decision is not None and args.next_decision.casefold() == "none"
            else args.next_decision
            if args.next_decision is not None
            else current.next_decision if current else ""
        )
        relationship = Relationship(
            key=args.key,
            name=args.name if args.name is not None else (current.name if current else args.key),
            stage=args.stage if args.stage is not None else (current.stage if current else "active"),
            last_interaction_at=current.last_interaction_at if current else None,
            next_decision=next_decision,
            resume_after=_optional_datetime_argument(args.resume_after, current.resume_after if current else None),
            cooling_off_until=_optional_datetime_argument(
                args.cooling_off_until,
                current.cooling_off_until if current else None,
            ),
            open_obligation_ids=current.open_obligation_ids if current else (),
            metadata=metadata,
        )
        ledger.upsert_relationship(relationship, reason="manual relationship correction")
        print(json.dumps(relationship.to_dict(), ensure_ascii=False, indent=2))
        return 0
    finally:
        ledger.close()


def _correct_gate(
    ledger: ObligationLedger,
    obligation,
    args: argparse.Namespace,
):
    timestamp = utc_now()
    gates = {gate.name: gate for gate in obligation.gates}
    previous = gates.get(args.name)
    gates[args.name] = Gate(
        name=args.name,
        state=args.state,
        owner=args.owner or (previous.owner if previous else obligation.owner),
        detail=args.detail or (previous.detail if previous else "operator correction"),
        required=previous.required if previous else True,
        evidence_ids=previous.evidence_ids if previous else (),
        updated_at=timestamp,
    )
    metadata = dict(obligation.metadata)
    manual = dict(metadata.get("manual_gates") or {})
    manual[args.name] = {
        "state": args.state,
        "owner": args.owner,
        "detail": args.detail,
        "required": previous.required if previous else True,
        "at": timestamp.isoformat(),
    }
    metadata["manual_gates"] = manual
    changed = replace(obligation, gates=tuple(gates.values()), metadata=metadata, updated_at=timestamp)
    ledger.upsert(changed, reason=f"manual gate correction: {args.name}")
    return changed


def _attach_evidence(
    ledger: ObligationLedger,
    obligation,
    args: argparse.Namespace,
):
    timestamp = utc_now()
    evidence = Evidence(
        id=f"manual:{args.category}:{int(timestamp.timestamp() * 1000)}",
        category=args.category,
        scope=args.scope,
        source="operator",
        owner="operator",
        detail=args.detail,
        observed_at=timestamp,
        expires_at=_optional_datetime_argument(args.expires_at, None),
    )
    changed = replace(
        obligation,
        evidence=(*obligation.evidence, evidence),
        updated_at=timestamp,
    )
    ledger.upsert(changed, reason=f"manual evidence attached: {args.category}")
    return changed


def _record_action(ledger: ObligationLedger, obligation, args: argparse.Namespace):
    timestamp = utc_now()
    metadata = dict(obligation.metadata)
    metadata["next_action"] = " ".join(str(args.text).split())
    if args.actor:
        previous = metadata.get("manual_correction")
        previous_fields = (
            previous.get("fields", [])
            if isinstance(previous, dict)
            else []
        )
        metadata["manual_correction"] = {
            "actor": "founderosctl",
            "at": timestamp.isoformat(),
            "fields": sorted({*(str(value) for value in previous_fields), "next_actor"}),
        }
    manual = dict(metadata.get("manual_gates") or {})
    manual["next_move"] = {
        "state": "satisfied",
        "owner": args.actor or obligation.next_actor,
        "detail": metadata["next_action"],
        "required": True,
        "at": timestamp.isoformat(),
    }
    metadata["manual_gates"] = manual
    gates = {gate.name: gate for gate in obligation.gates}
    previous = gates.get("next_move")
    gates["next_move"] = Gate(
        name="next_move",
        state="satisfied",
        owner=args.actor or (previous.owner if previous else obligation.next_actor),
        detail=metadata["next_action"],
        required=previous.required if previous else True,
        evidence_ids=previous.evidence_ids if previous else (),
        updated_at=timestamp,
    )
    changed = replace(
        obligation,
        next_actor=args.actor or obligation.next_actor,
        gates=tuple(gates.values()),
        metadata=metadata,
        updated_at=timestamp,
    )
    ledger.upsert(changed, reason="manual next action recorded")
    return changed


def _obligation_summary(obligation) -> dict:
    return {
        "id": obligation.id,
        "state": obligation.state,
        "priority": obligation.priority,
        "title": obligation.title,
        "owner": obligation.owner,
        "next_actor": obligation.next_actor,
        "due_at": obligation.due_at.isoformat() if obligation.due_at else None,
        "missing_gates": [gate.name for gate in obligation.missing_gates],
        "evidence_count": len(obligation.evidence),
    }


def _obligation_ledger(config: dict) -> ObligationLedger:
    closure = config["closure"]
    return ObligationLedger(
        closure["ledger_path"],
        audit_max_entries=closure.get("audit_max_entries", 100_000),
    )


def _required_datetime(value: str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError("an ISO-8601 timestamp is required")
    return parsed


def _optional_datetime_argument(value: str | None, current: datetime | None) -> datetime | None:
    if not value:
        return current
    if value.casefold() == "none":
        return None
    return _required_datetime(value)


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
        scopes = list(google_scopes_for_connectors(config["connectors"]))
        if args.include_drive and GOOGLE_DRIVE_SCOPE not in scopes:
            scopes.append(GOOGLE_DRIVE_SCOPE)
        if args.include_sheets and GOOGLE_SHEETS_SCOPE not in scopes:
            scopes.append(GOOGLE_SHEETS_SCOPE)
        result = authorize_google(
            args.client_json,
            store,
            scopes=scopes,
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


def _configured_display(
    config: dict,
    *,
    application_suffix: str = "",
    priority: int | None = None,
    text_rendering: str | None = None,
) -> BusyBarDisplay:
    display_config = config["display"]
    token_account = str(display_config.get("api_token_env", "")).strip()
    resolver = build_secret_resolver(config["secrets"])
    return BusyBarDisplay(
        str(display_config["host"]),
        application_name=str(display_config["application_name"]) + application_suffix,
        priority=priority if priority is not None else int(display_config["device_priority"]),
        timeout=float(display_config["request_timeout_seconds"]),
        api_token=resolver.get(token_account) if token_account else "",
        api_semver=str(display_config["api_semver"]),
        text_rendering=text_rendering or str(display_config["text_rendering"]),
        font_atlas_path=str(REPO_ROOT / "public" / "fonts" / "font-atlas.json"),
    )


def _display_command(args: argparse.Namespace, config: dict) -> int:
    if args.display_command == "status":
        display = _configured_display(config)
        front = display.screen(0)
        back = display.screen(1)
        print(json.dumps({
            "capabilities": display.capabilities().as_dict(),
            "screen": {
                "front": {"width": front.width, "height": front.height, "mode": front.mode, "bytes": len(front.pixels)},
                "back": {"width": back.width, "height": back.height, "mode": back.mode, "bytes": len(back.pixels)},
            },
        }, ensure_ascii=False, indent=2))
        return 0
    mode = "raster_non_ascii" if args.raster_fallback else "native"
    display = _configured_display(
        config,
        application_suffix="-glyph-check",
        priority=100,
        text_rendering=mode,
    )
    result = verify_french_glyphs(
        display,
        REPO_ROOT / "public" / "fonts" / "font-atlas.json",
    )
    payload = result.as_dict()
    payload["rendering"] = mode
    if not result.passed and mode == "native":
        payload["recommended_text_rendering"] = "raster_non_ascii"
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.passed else 4


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
    display = _configured_display(config)
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
