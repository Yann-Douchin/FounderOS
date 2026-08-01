#!/usr/bin/env python3
"""Send one request-bound input to the authenticated FounderOS bridge."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
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
    parser.add_argument("--secret-env", default="FOUNDEROS_INPUT_SECRET")
    parser.add_argument("--context", action="store_true", help="Print the current bound context only")
    parser.add_argument("--generate-secret", action="store_true", help="Generate a new 256-bit bridge secret")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.generate_secret:
        print(secrets.token_urlsafe(32))
        return 0
    secret = os.environ.get(args.secret_env, "").strip()
    if not secret:
        print(f"missing {args.secret_env}", file=sys.stderr)
        return 2
    try:
        context = read_context(args.url, secret)
        if args.context:
            print(json.dumps(context, ensure_ascii=False, sort_keys=True))
            return 0
        if not args.key:
            print("a key is required unless --context is used", file=sys.stderr)
            return 2
        payload = {
            "key": args.key,
            "event_id": str(context.get("event_id", "")),
            "request_id": str(context.get("request_id", "")),
            "issued_at": int(time.time()),
            "nonce": secrets.token_urlsafe(24),
        }
        result = send_input(args.url, secret, payload)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"input failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def read_context(base_url: str, secret: str) -> dict[str, object]:
    _require_loopback(base_url)
    request = urllib.request.Request(
        base_url.rstrip("/") + "/context",
        headers={"Authorization": "Bearer " + secret, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=3.0) as response:
        payload = json.loads(response.read().decode("utf-8"))
    context = payload.get("context") if isinstance(payload, dict) else None
    if not isinstance(context, dict) or not context.get("event_id"):
        raise ValueError("FounderOS has no selected event")
    return context


def send_input(base_url: str, secret: str, payload: dict[str, object]) -> dict[str, object]:
    _require_loopback(base_url)
    body = encode_signed_payload(payload)
    request = urllib.request.Request(
        base_url.rstrip("/") + "/input",
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
