#!/usr/bin/env python3
"""Route one Claude or Codex PermissionRequest through FounderOS."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from founder_os.agents.bridge import AgentBridgeError, BridgeStore  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=("claude", "chatgpt_codex"))
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("FOUNDEROS_AGENT_STATE_DIR", ".data/agents"),
    )
    parser.add_argument("--timeout", type=float, default=45.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "PermissionRequest":
            print("{}")
            return 0
        store = BridgeStore(args.state_dir)
        request = store.create_permission_request(
            args.provider,
            payload,
            timeout_seconds=args.timeout,
        )
        decision = store.wait_for_decision(
            args.provider,
            str(request["request_id"]),
            timeout_seconds=args.timeout,
        )
    except (AgentBridgeError, OSError, ValueError, json.JSONDecodeError):
        print("{}")
        return 0

    if decision == "allow":
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        }
    elif decision == "deny":
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {
                    "behavior": "deny",
                    "message": "Refusé depuis le BUSY Bar.",
                },
            }
        }
    else:
        result = {}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
