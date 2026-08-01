#!/usr/bin/env python3
"""Inspect and feed the local FounderOS agent bridge."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from founder_os.agents.bridge import AgentBridgeError, BridgeStore  # noqa: E402
from founder_os.paths import agent_state_root  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=str(agent_state_root()))
    commands = parser.add_subparsers(dest="command", required=True)

    usage = commands.add_parser("usage", help="Publish a supported usage snapshot")
    usage.add_argument("--provider", required=True, choices=("claude", "chatgpt_codex"))
    usage.add_argument(
        "--window",
        action="append",
        required=True,
        metavar="LABEL:USED_PERCENT",
        help="Repeat once per quota window, for example 5H:42 or SEM:68",
    )
    usage.add_argument("--ttl-seconds", type=float, default=900)
    usage.add_argument("--plan-type", default="")

    request = commands.add_parser("request", help="Create a non-blocking display test request")
    request.add_argument("--provider", required=True, choices=("claude", "chatgpt_codex"))
    request.add_argument("--tool", default="Bash")
    request.add_argument("--summary", required=True)
    request.add_argument("--ttl-seconds", type=float, default=120)

    pending = commands.add_parser("pending", help="List pending requests")
    pending.add_argument("--provider", required=True, choices=("claude", "chatgpt_codex"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = BridgeStore(args.state_dir)
    try:
        if args.command == "usage":
            windows = [_parse_window(value) for value in args.window]
            result = store.publish_usage(
                args.provider,
                windows,
                ttl_seconds=args.ttl_seconds,
                plan_type=args.plan_type,
            )
        elif args.command == "request":
            result = store.create_permission_request(
                args.provider,
                {
                    "hook_event_name": "PermissionRequest",
                    "tool_name": args.tool,
                    "tool_input": {"description": args.summary},
                },
                timeout_seconds=args.ttl_seconds,
            )
        else:
            result = {"requests": store.pending_requests(args.provider)}
    except AgentBridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _parse_window(value: str) -> dict[str, object]:
    label, separator, percent = value.partition(":")
    if not separator:
        raise AgentBridgeError(f"invalid usage window: {value}")
    try:
        used_percent = float(percent)
    except ValueError as exc:
        raise AgentBridgeError(f"invalid usage percentage: {percent}") from exc
    return {"label": label, "used_percent": used_percent}


if __name__ == "__main__":
    raise SystemExit(main())
