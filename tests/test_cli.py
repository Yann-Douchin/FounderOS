from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from apps.founderos import parse_args, unhealthy_connectors


class FounderOSCLITests(unittest.TestCase):
    def test_configured_display_host_is_not_overwritten_by_a_cli_default(self) -> None:
        with patch.object(sys, "argv", ["founderos.py", "--config", "founderos.local.json"]):
            args = parse_args()
        self.assertIsNone(args.host)

    def test_health_preflight_can_cover_all_or_only_critical_connectors(self) -> None:
        health = {
            "linear": {"status": "healthy", "critical": True},
            "gmail": {"status": "degraded", "critical": True},
            "claude": {"status": "starting", "critical": False},
        }
        self.assertEqual(
            unhealthy_connectors(health, critical_only=True),
            ["gmail"],
        )
        self.assertEqual(
            unhealthy_connectors(health, critical_only=False),
            ["claude", "gmail"],
        )


if __name__ == "__main__":
    unittest.main()
