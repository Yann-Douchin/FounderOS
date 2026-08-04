#!/usr/bin/env python3
"""Send one request-bound input to the authenticated FounderOS bridge."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import socket
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from founder_os.interaction import encode_signed_payload, signature_for  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("key", nargs="?", help="BUSY Bar key name, for example ok or back")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--config", default="founderos.autonomous.local.json")
    parser.add_argument(
        "--local-socket",
        default=str(Path.home() / "Library/Application Support/FounderOS/founderos-input.sock"),
    )
    parser.add_argument("--secret-env", default="FOUNDEROS_INPUT_SECRET")
    parser.add_argument("--context", action="store_true", help="Print the current bound context only")
    parser.add_argument(
        "--lease-action",
        choices=("acquire", "renew", "release", "release_all"),
        help="Manage one Stream Deck occupancy lease",
    )
    parser.add_argument("--lease-id", default="", help="Stable local lease identifier")
    parser.add_argument(
        "--state",
        choices=("focus", "manual_call", "recording"),
        help="Presence state for lease acquisition",
    )
    parser.add_argument("--ttl-seconds", type=int, help="Bounded TTL for acquisition or renewal")
    parser.add_argument("--generate-secret", action="store_true", help="Generate a new 256-bit bridge secret")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.generate_secret:
        print(secrets.token_urlsafe(32))
        return 0
    try:
        socket_path = Path(args.local_socket).expanduser()
        if not socket_path.is_absolute():
            raise ValueError("FounderOS local socket path must be absolute")
        use_local_socket = local_socket_available(socket_path)
        secret = "" if use_local_socket else resolve_secret(args.config, args.secret_env)
        if not use_local_socket and not secret:
            print(f"missing {args.secret_env}", file=sys.stderr)
            return 2
        context = (
            read_local_context(socket_path)
            if use_local_socket
            else read_context(args.url, secret)
        )
        if args.context:
            print(json.dumps(context, ensure_ascii=False, sort_keys=True))
            return 0
        if args.lease_action:
            payload = presence_payload(args)
            result = (
                send_local(socket_path, "presence", payload)
                if use_local_socket
                else send_signed(args.url, secret, "/presence/lease", payload)
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if not args.key:
            print("a key is required unless --context is used", file=sys.stderr)
            return 2
        if not context.get("event_id"):
            raise ValueError("FounderOS has no visible event")
        payload = {
            "key": args.key,
            "event_id": str(context.get("event_id", "")),
            "request_id": str(context.get("request_id", "")),
            "issued_at": int(time.time()),
            "nonce": secrets.token_urlsafe(24),
        }
        result = (
            send_local(socket_path, "input", payload)
            if use_local_socket
            else send_signed(args.url, secret, "/input", payload)
        )
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"input failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def resolve_secret(config_path: str, account: str) -> str:
    from founder_os.config import load_config
    from founder_os.secrets import build_secret_resolver

    account = str(account).strip()
    config_file = Path(config_path).expanduser()
    try:
        config = load_config(config_file)
    except ValueError:
        # Preserve the standalone development fallback, but never let an
        # ambient value override an existing, invalid configuration.
        return "" if config_file.exists() else os.environ.get(account, "").strip()
    configured_account = str(config["interaction"].get("secret_env") or account).strip()
    if account != "FOUNDEROS_INPUT_SECRET" and configured_account != account:
        return ""
    return build_secret_resolver(config["secrets"]).get(configured_account)


def local_socket_available(socket_path: Path) -> bool:
    try:
        parent = socket_path.parent.lstat()
        facts = socket_path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise ValueError("FounderOS local bridge parent is not private")
    if (
        not stat.S_ISSOCK(facts.st_mode)
        or facts.st_uid != os.getuid()
        or stat.S_IMODE(facts.st_mode) != 0o600
    ):
        raise ValueError("FounderOS local bridge is not a private account-owned socket")
    return True


def read_local_context(socket_path: Path) -> dict[str, object]:
    response = local_request(socket_path, {"version": 1, "operation": "context"})
    context = response.get("context")
    if not isinstance(context, dict):
        raise ValueError("FounderOS returned an invalid local context")
    return context


def send_local(
    socket_path: Path,
    operation: str,
    payload: dict[str, object],
) -> dict[str, object]:
    if operation not in {"input", "presence"}:
        raise ValueError("unsupported FounderOS local bridge operation")
    return local_request(
        socket_path,
        {"version": 1, "operation": operation, "payload": payload},
    )


def local_request(socket_path: Path, request: dict[str, object]) -> dict[str, object]:
    if not local_socket_available(socket_path):
        raise OSError("FounderOS local bridge is unavailable")
    body = (
        json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(body) > 4096:
        raise ValueError("FounderOS local request is too large")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(3.0)
    try:
        connection.connect(str(socket_path))
        connection.sendall(body)
        response = bytearray()
        while len(response) <= 65536:
            block = connection.recv(min(4096, 65537 - len(response)))
            if not block:
                break
            response.extend(block)
            if response.endswith(b"\n"):
                break
    finally:
        connection.close()
    if not response.endswith(b"\n") or len(response) > 65536:
        raise ValueError("FounderOS returned an invalid local response")
    result = json.loads(bytes(response).decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("FounderOS returned a non-object local response")
    error = result.get("error")
    if isinstance(error, str) and error:
        raise ValueError(f"FounderOS local bridge rejected the request: {error}")
    return result


def read_context(base_url: str, secret: str) -> dict[str, object]:
    _require_loopback(base_url)
    request = urllib.request.Request(
        base_url.rstrip("/") + "/context",
        headers={"Authorization": "Bearer " + secret, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=3.0) as response:
        payload = json.loads(response.read().decode("utf-8"))
    context = payload.get("context") if isinstance(payload, dict) else None
    if not isinstance(context, dict):
        raise ValueError("FounderOS returned an invalid context")
    return context


def send_input(base_url: str, secret: str, payload: dict[str, object]) -> dict[str, object]:
    """Compatibility wrapper for clients importing the original helper."""
    return send_signed(base_url, secret, "/input", payload)


def send_signed(
    base_url: str,
    secret: str,
    path: str,
    payload: dict[str, object],
) -> dict[str, object]:
    _require_loopback(base_url)
    if path not in {"/input", "/presence/lease"}:
        raise ValueError("unsupported FounderOS bridge path")
    body = encode_signed_payload(payload)
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-FounderOS-Signature": signature_for(secret, body),
        },
    )
    with urllib.request.urlopen(request, timeout=3.0) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("FounderOS returned a non-object response")
    return result


def presence_payload(args: argparse.Namespace) -> dict[str, object]:
    action = str(args.lease_action)
    payload: dict[str, object] = {
        "action": action,
        "issued_at": int(time.time()),
        "nonce": secrets.token_urlsafe(24),
    }
    if action != "release_all":
        if not args.lease_id:
            raise ValueError("--lease-id is required for this lease action")
        payload["lease_id"] = args.lease_id
    if action == "acquire":
        if not args.state:
            raise ValueError("--state is required when acquiring a lease")
        payload["state"] = args.state
    elif args.state:
        raise ValueError("--state is accepted only when acquiring a lease")
    if action in {"acquire", "renew"}:
        if args.ttl_seconds is None:
            raise ValueError("--ttl-seconds is required for this lease action")
        payload["ttl_seconds"] = args.ttl_seconds
    elif args.ttl_seconds is not None:
        raise ValueError("--ttl-seconds is not accepted for this lease action")
    return payload


def _require_loopback(base_url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(base_url)
        hostname = parsed.hostname or ""
    except ValueError as exc:
        raise ValueError("input bridge URL is invalid") from exc
    if parsed.scheme != "http" or not hostname or parsed.username or parsed.password:
        raise ValueError("input bridge URL must be a credential-free loopback HTTP endpoint")
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname == "localhost"
    if not is_loopback:
        raise ValueError("input bridge URL must use loopback")


if __name__ == "__main__":
    raise SystemExit(main())
