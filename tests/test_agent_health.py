from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from founder_os.agents.codex import CodexAppServerError
from founder_os.connectors.agents import AgentBridgeConnector


NOW = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)


class AgentHealthTests(unittest.TestCase):
    def test_missing_codex_app_server_is_a_visible_degraded_event(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch(
            "founder_os.connectors.agents.CodexAppServerClient",
            side_effect=CodexAppServerError("Codex executable unavailable"),
        ):
            connector = AgentBridgeConnector(
                {
                    "state_dir": folder,
                    "usage": {"mode": "codex_app_server", "refresh_seconds": 60},
                },
                source="chatgpt_codex",
            )
            events = connector.poll(NOW)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, "connector_health")
            self.assertIn("unavailable", events[0].title)
            self.assertEqual(events[0].metadata["component"], "usage")

    def test_usage_dedupe_is_provider_specific(self) -> None:
        connector = AgentBridgeConnector.__new__(AgentBridgeConnector)
        connector.source = "claude"
        event = connector._usage_event(
            {
                "updated_at": NOW.isoformat(),
                "expires_at": NOW.replace(hour=11).isoformat(),
                "windows": [{"label": "5H", "used_percent": 20}],
            }
        )
        self.assertEqual(event.dedupe_key, "claude:usage")


if __name__ == "__main__":
    unittest.main()
