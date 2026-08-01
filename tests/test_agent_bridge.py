from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from founder_os.agents.bridge import BridgeStore, summarize_permission
from founder_os.agents.codex import normalize_rate_limits
from founder_os.config import load_config
from founder_os.connectors.agents import AgentBridgeConnector
from founder_os.connectors.registry import build_connectors
from founder_os.core.runtime import FounderOSRuntime
from founder_os.display.busybar import RecordingDisplay
from founder_os.interaction import parse_sse


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
NOW = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


class AgentBridgeTests(unittest.TestCase):
    def test_permission_summary_preserves_accents_and_redacts_secrets(self) -> None:
        tool, summary = summarize_permission(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "Déployer avec api_key=sk-supersecret123456"},
            }
        )
        self.assertEqual(tool, "Bash")
        self.assertIn("Déployer", summary)
        self.assertIn("[secret masqué]", summary)
        self.assertNotIn("supersecret", summary)

    def test_request_decision_round_trip_is_atomic_and_ephemeral(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = BridgeStore(folder)
            request = store.create_permission_request(
                "claude",
                {
                    "tool_name": "Bash",
                    "tool_input": {"description": "Lancer les tests ?"},
                },
                timeout_seconds=30,
                now=NOW,
            )
            request_id = str(request["request_id"])
            self.assertEqual(store.pending_requests("claude", now=NOW)[0]["summary"], "Lancer les tests ?")
            self.assertTrue(store.decide("claude", request_id, "allow", now=NOW))
            self.assertEqual(store.pending_requests("claude", now=NOW), [])
            self.assertEqual(
                store.wait_for_decision("claude", request_id, timeout_seconds=0.2, poll_seconds=0.01),
                "allow",
            )
            self.assertFalse((Path(folder) / "claude" / "requests" / f"{request_id}.json").exists())

    def test_usage_snapshot_becomes_a_low_priority_event(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = BridgeStore(folder)
            store.publish_usage(
                "claude",
                [
                    {"label": "5H", "used_percent": 32},
                    {"label": "SEM", "used_percent": 71},
                ],
                ttl_seconds=120,
                now=NOW,
            )
            connector = AgentBridgeConnector(
                {
                    "state_dir": folder,
                    "usage": {"mode": "snapshot"},
                    "poll_interval_seconds": 1,
                },
                source="claude",
            )
            events = connector.poll(NOW)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, "agent_usage")
            self.assertEqual(events[0].title, "Utilisation Claude")
            self.assertEqual(events[0].metadata["windows"][1]["label"], "SEM")

    def test_codex_rate_limits_use_official_primary_and_secondary_windows(self) -> None:
        record = normalize_rate_limits(
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "planType": "pro",
                        "primary": {
                            "usedPercent": 25,
                            "windowDurationMins": 300,
                            "resetsAt": 1785582000,
                        },
                        "secondary": {
                            "usedPercent": 42,
                            "windowDurationMins": 10080,
                            "resetsAt": 1786186800,
                        },
                    }
                }
            },
            now=NOW,
        )
        self.assertIsNotNone(record)
        self.assertEqual(record["plan_type"], "pro")
        self.assertEqual([item["label"] for item in record["windows"]], ["5H", "SEM"])
        self.assertEqual([item["used_percent"] for item in record["windows"]], [25.0, 42.0])

    def test_registry_builds_agent_bridge_without_agent_credentials(self) -> None:
        connectors = build_connectors(
            {
                "claude": {
                    "enabled": True,
                    "mode": "agent_bridge",
                    "state_dir": ".data/agents",
                    "usage": {"mode": "disabled"},
                }
            }
        )
        self.assertEqual(len(connectors), 1)
        self.assertIsInstance(connectors[0], AgentBridgeConnector)

    def test_runtime_maps_only_selected_approval_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = BridgeStore(folder)
            request = store.create_permission_request(
                "claude",
                {"tool_name": "Bash", "tool_input": {"description": "Publier la version ?"}},
                timeout_seconds=60,
            )
            config = load_config(
                overrides={
                    "memory": {"path": str(Path(folder) / "memory.json")},
                    "connectors": {
                        "claude": {
                            "enabled": True,
                            "mode": "agent_bridge",
                            "state_dir": folder,
                            "usage": {"mode": "disabled"},
                        }
                    },
                }
            )
            runtime = FounderOSRuntime(config, display=RecordingDisplay())
            state = runtime.tick(force_poll=True)
            self.assertEqual(state.selected.event.kind, "permission_request")
            self.assertIsNone(runtime.handle_input("up"))
            self.assertEqual(runtime.handle_input("back"), "deny")
            self.assertEqual(
                store.wait_for_decision(
                    "claude",
                    str(request["request_id"]),
                    timeout_seconds=0.2,
                    poll_seconds=0.01,
                ),
                "deny",
            )

    def test_emulator_sse_parser_keeps_only_complete_events(self) -> None:
        events = list(
            parse_sse(
                [
                    b"event: state\n",
                    b'data: {"display":{}}\n',
                    b"\n",
                    b"event: input\n",
                    b'data: {"key":"ok"}\n',
                    b"\n",
                ]
            )
        )
        self.assertEqual(events[-1], ("input", {"key": "ok"}))

    def test_hook_returns_exact_decision_shape_for_both_agents(self) -> None:
        for provider in ("claude", "chatgpt_codex"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                store = BridgeStore(folder)
                stop = threading.Event()

                def answer() -> None:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and not stop.is_set():
                        request_id = store.latest_request_id(provider)
                        if request_id:
                            store.decide(provider, request_id, "allow")
                            return
                        time.sleep(0.01)

                responder = threading.Thread(target=answer)
                responder.start()
                process = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "apps" / "agent_permission_hook.py"),
                        "--provider",
                        provider,
                        "--state-dir",
                        folder,
                        "--timeout",
                        "2",
                    ],
                    input=json.dumps(
                        {
                            "hook_event_name": "PermissionRequest",
                            "tool_name": "Bash",
                            "tool_input": {"command": "npm test"},
                        }
                    ),
                    text=True,
                    capture_output=True,
                    timeout=4,
                    check=True,
                )
                stop.set()
                responder.join(timeout=1)
                self.assertEqual(process.stderr, "")
                self.assertEqual(
                    json.loads(process.stdout),
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PermissionRequest",
                            "decision": {"behavior": "allow"},
                        }
                    },
                )


if __name__ == "__main__":
    unittest.main()
