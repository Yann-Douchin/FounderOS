#!/usr/bin/env python3
"""Verify FounderOS against the pinned BarPilot API 25 observations."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from founder_os.display.busybar import BusyBarDisplay, DisplayConflict  # noqa: E402
from founder_os.config import load_config  # noqa: E402
from founder_os.secrets import build_secret_resolver  # noqa: E402


CONTRACT = json.loads(
    (ROOT / "tests" / "fixtures" / "barpilot-api25-contract.json").read_text(encoding="utf-8")
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="", help="BUSY Bar host, defaults to the config value or 127.0.0.1:8080")
    parser.add_argument("--config", help="Optional FounderOS config used to resolve host and API token safely")
    parser.add_argument("--source", help="Optional local barpilot.html to verify against the pinned commit")
    parser.add_argument("--source-only", action="store_true", help="Verify the source file without opening an API connection")
    parser.add_argument("--mutating", action="store_true", help="Run controlled draw, merge, and priority checks")
    parser.add_argument(
        "--emulator-blockers",
        action="store_true",
        help="Exercise FounderOS emulator-only physical-session, menu, and smart-home blockers",
    )
    parser.add_argument(
        "--full-emulator-api",
        action="store_true",
        help="Exercise all 53 paths and 69 operations after proving the target is the FounderOS emulator",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    host = args.host or "127.0.0.1:8080"
    token = ""
    if args.config:
        config = load_config(args.config)
        display_config = config["display"]
        host = args.host or str(display_config["host"])
        account = str(display_config.get("api_token_env", "")).strip()
        token = build_secret_resolver(config["secrets"]).get(account) if account else ""
    report: dict[str, Any] = {"reference": CONTRACT["reference"], "checks": {}}
    if args.source:
        report["checks"]["source"] = verify_source(Path(args.source))
    if args.source_only:
        if not args.source:
            raise SystemExit("--source-only requires --source")
        passed = bool(report["checks"]["source"]["passed"])
        report["passed"] = passed
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if passed else 4
    display = BusyBarDisplay(host, api_token=token, application_name="barpilot-compat-a", priority=97)
    capabilities = display.capabilities()
    front = display.screen(0)
    back = display.screen(1)
    report["checks"]["read_only"] = {
        "api_semver": display.version(),
        "capability_profile": capabilities.profile,
        "front_bytes": len(front.pixels),
        "back_bytes": len(back.pixels),
        "status_websocket": verify_status_websocket(host, token),
        "passed": len(front.pixels) == 72 * 16 * 3 and len(back.pixels) == 80 * 80,
    }
    report["checks"]["read_only"]["passed"] = bool(
        report["checks"]["read_only"]["passed"]
        and report["checks"]["read_only"]["status_websocket"]
    )
    if args.mutating:
        report["checks"]["draw_contract"] = verify_draw_contract(host, token)
    if args.emulator_blockers:
        report["checks"]["emulator_blockers"] = verify_emulator_blockers(host, token)
    if args.full_emulator_api:
        report["checks"]["full_emulator_api"] = verify_full_emulator_api(host, token)
    passed = all(bool(check.get("passed")) for check in report["checks"].values())
    report["passed"] = passed
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 4


def verify_source(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    text = data.decode("utf-8")
    markers = ["/api/screen", "/api/status/ws", "blocks ALL canvas draws", "bgInterval"]
    missing = [marker for marker in markers if marker not in text]
    source_operations = extract_source_operations(text)
    contract_operations = {
        (method, str(endpoint["path"]))
        for endpoint in CONTRACT["barpilot_endpoints"]
        for method in endpoint["methods"]
    }
    missing_operations = sorted(contract_operations - source_operations)
    unexpected_operations = sorted(source_operations - contract_operations)
    return {
        "path": str(path),
        "sha256": digest,
        "expected_sha256": CONTRACT["reference"]["sha256"],
        "missing_markers": missing,
        "source_endpoint_count": len({operation[1] for operation in source_operations}),
        "source_operation_count": len(source_operations),
        "missing_operations": missing_operations,
        "unexpected_operations": unexpected_operations,
        "passed": (
            digest == CONTRACT["reference"]["sha256"]
            and not missing
            and not missing_operations
            and not unexpected_operations
        ),
    }


def extract_source_operations(text: str) -> set[tuple[str, str]]:
    match = re.search(r"const ENDPOINTS\s*=\s*\[(.*?)\n\s*\];", text, re.DOTALL)
    if not match:
        return set()
    operations: set[tuple[str, str]] = set()
    for method, raw_path in re.findall(
        r"\['(GET|POST|PUT|DELETE)'\s*,\s*'([^']+)'\]",
        match.group(1),
    ):
        operations.add((method, raw_path.split("?", 1)[0]))
    return operations


def verify_status_websocket(host: str, token: str = "") -> bool:
    if "\r" in token or "\n" in token:
        raise ValueError("API token contains an invalid header character")
    base = host if host.startswith(("http://", "https://")) else "http://" + host
    parsed = urllib.parse.urlsplit(base)
    hostname = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection = socket.create_connection((hostname, port), timeout=3)
    if parsed.scheme == "https":
        connection = ssl.create_default_context().wrap_socket(connection, server_hostname=hostname)
    connection.settimeout(3)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET /api/status/ws HTTP/1.1\r\n"
        + f"Host: {hostname}:{port}\r\n"
        + "Upgrade: websocket\r\n"
        + "Connection: Upgrade\r\n"
        + f"Sec-WebSocket-Key: {key}\r\n"
        + "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    if token:
        request = request.replace("\r\n\r\n", f"\r\nX-API-Token: {token}\r\n\r\n")
    try:
        connection.sendall(request.encode("ascii"))
        headers = receive_until(connection, b"\r\n\r\n", 8192)
        if not headers.startswith(b"HTTP/1.1 101"):
            return False
        connection.sendall(masked_text_frame('{"enable":true}'))
        first = receive_exact(connection, 2)
        length = first[1] & 0x7F
        if length == 126:
            length = int.from_bytes(receive_exact(connection, 2), "big")
        elif length == 127:
            length = int.from_bytes(receive_exact(connection, 8), "big")
        payload = receive_exact(connection, length)
        message = json.loads(payload.decode("utf-8"))
        return isinstance(message, dict) and isinstance(message.get("frame"), dict)
    finally:
        connection.close()


def masked_text_frame(value: str) -> bytes:
    payload = value.encode("utf-8")
    mask = os.urandom(4)
    if len(payload) < 126:
        header = bytes([0x81, 0x80 | len(payload)])
    else:
        header = bytes([0x81, 0x80 | 126]) + len(payload).to_bytes(2, "big")
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return header + mask + masked


def receive_exact(connection: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise RuntimeError("status WebSocket closed before a complete frame")
        chunks.extend(chunk)
    return bytes(chunks)


def receive_until(connection: socket.socket, marker: bytes, limit: int) -> bytes:
    chunks = bytearray()
    while marker not in chunks:
        chunk = connection.recv(1024)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise RuntimeError("status WebSocket handshake exceeded its limit")
    return bytes(chunks)


def verify_draw_contract(host: str, token: str) -> dict[str, Any]:
    first = BusyBarDisplay(host, api_token=token, application_name="barpilot-compat-a", priority=97)
    equal = BusyBarDisplay(host, api_token=token, application_name="barpilot-compat-b", priority=97)
    higher = BusyBarDisplay(host, api_token=token, application_name="barpilot-compat-b", priority=98)
    equal_rejected = False
    try:
        first.draw([pixel("left", 0, "0xFF0000FF")])
        first.draw([pixel("right", 1, "0x00FF00FF")])
        capture = first.screen(0)
        merged = capture.pixel(0, 0) == (255, 0, 0) and capture.pixel(1, 0) == (0, 255, 0)
        try:
            equal.draw([pixel("equal", 2, "0xFFFFFFFF")])
        except DisplayConflict:
            equal_rejected = True
        higher.draw([pixel("higher", 2, "0x0000FFFF")])
        taken_over = higher.screen(0).pixel(2, 0) == (0, 0, 255)
        return {
            "same_app_equal_priority_merge": merged,
            "different_app_equal_priority_rejected": equal_rejected,
            "different_app_higher_priority_takeover": taken_over,
            "passed": merged and equal_rejected and taken_over,
        }
    finally:
        try:
            higher.clear()
        finally:
            first.clear()


def verify_emulator_blockers(host: str, token: str) -> dict[str, Any]:
    display = BusyBarDisplay(host, api_token=token, application_name="barpilot-compat-blocker", priority=100)
    outcomes: dict[str, bool] = {}
    set_busy_timer(host, token, False)
    set_busy_timer(host, token, True)
    try:
        try:
            display.draw([pixel("probe", 0, "0xFFFFFFFF")])
        except DisplayConflict:
            outcomes["api_busy_timer"] = True
        else:
            outcomes["api_busy_timer"] = False
    finally:
        set_busy_timer(host, token, False)
    mapping = {"physical_busy": "physical_busy", "menu": "menu", "smart_home": "smart_home"}
    for label, blocker_type in mapping.items():
        set_blocker(host, token, blocker_type, True)
        try:
            if label == "physical_busy":
                snapshot = api_json(host, token, "GET", "/api/busy/snapshot")
                outcomes["physical_hidden_from_snapshot"] = (
                    snapshot.get("snapshot", {}).get("type") == "NOT_STARTED"
                )
            try:
                display.draw([pixel("probe", 0, "0xFFFFFFFF")])
            except DisplayConflict:
                outcomes[label] = True
            else:
                outcomes[label] = False
        finally:
            set_blocker(host, token, blocker_type, False)
    outcomes["passed"] = all(outcomes.values())
    return outcomes


def verify_full_emulator_api(host: str, token: str) -> dict[str, Any]:
    base = host if host.startswith(("http://", "https://")) else "http://" + host
    headers = {"X-API-Sem-Ver": "25.0.0"}
    if token:
        headers["X-API-Token"] = token
    request = urllib.request.Request(base.rstrip("/") + "/api/_scenario", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            marker = json.loads(response.read(64 * 1024).decode("utf-8"))
    except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        return {"passed": False, "error": f"target is not a confirmed FounderOS emulator: {exc}"}
    if not isinstance(marker, dict) or "blockers" not in marker:
        return {"passed": False, "error": "target is not a confirmed FounderOS emulator"}
    environment = dict(os.environ)
    if token:
        environment["BARPILOT_API_TOKEN"] = token
    try:
        result = subprocess.run(
            ["node", str(ROOT / "tests" / "barpilot_endpoint_matrix_test.js"), base.rstrip("/")],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"passed": False, "error": str(exc)}
    return {
        "endpoint_count": 53,
        "operation_count": 69,
        "output": result.stdout.strip()[-1000:],
        "error": result.stderr.strip()[-1000:],
        "passed": result.returncode == 0,
    }


def pixel(element_id: str, x: int, color: str) -> dict[str, Any]:
    return {
        "id": element_id,
        "type": "rectangle",
        "x": x,
        "y": 0,
        "width": 1,
        "height": 1,
        "border_width": 0,
        "fill": "solid",
        "fill_colors": [color],
    }


def set_blocker(host: str, token: str, blocker_type: str, active: bool) -> None:
    api_json(
        host,
        token,
        "POST",
        "/api/_scenario/blocker",
        {"type": blocker_type, "active": active},
    )


def set_busy_timer(host: str, token: str, active: bool) -> None:
    snapshot: dict[str, Any] = {
        "type": "SIMPLE" if active else "NOT_STARTED",
        "busy_bar_settings": {
            "theme": "busy",
            "show_work_phase_only": False,
            "trigger_smart_home": True,
        },
    }
    if active:
        snapshot.update({"is_paused": False, "time_left_ms": 60_000})
    api_json(
        host,
        token,
        "PUT",
        "/api/busy/snapshot",
        {"snapshot": snapshot, "snapshot_timestamp_ms": 1_785_628_800_000},
    )


def api_json(
    host: str,
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = host if host.startswith(("http://", "https://")) else "http://" + host
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"X-API-Sem-Ver": "25.0.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-API-Token"] = token
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            raw = response.read(64 * 1024)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"emulator API request failed: {exc.reason}") from exc
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("emulator API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("emulator API returned a non-object JSON response")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
