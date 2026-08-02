from __future__ import annotations

import argparse
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from apps.founderos import parse_args, runtime_overrides, unhealthy_connectors
from apps.founderosctl import _display_command


class FounderOSCLITests(unittest.TestCase):
    def test_demo_and_dry_run_never_activate_physical_automations(self) -> None:
        with patch.object(sys, "argv", ["founderos.py", "--demo"]):
            demo_args = parse_args()
        with patch.object(sys, "argv", ["founderos.py", "--dry-run"]):
            dry_run_args = parse_args()
        self.assertFalse(
            runtime_overrides(demo_args)["automations"]["calendar_busy_indicator"]["enabled"]
        )
        self.assertFalse(
            runtime_overrides(dry_run_args)["automations"]["calendar_busy_indicator"]["enabled"]
        )

    def test_matter_status_is_read_only_and_reports_commissioning(self) -> None:
        class FakeDisplay:
            def smart_home_pairing(self):
                return {
                    "fabric_count": 1,
                    "latest_pairing_status": {"value": "completed_successfully"},
                    "qr_code": "must-not-leak",
                }

            def smart_home_switch(self):
                return {"state": False}

        output = io.StringIO()
        args = argparse.Namespace(display_command="matter-status")
        with (
            patch("apps.founderosctl._configured_calendar_indicator", return_value=FakeDisplay()),
            redirect_stdout(output),
        ):
            exit_code = _display_command(args, {})
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["commissioned"])
        self.assertEqual(payload["fabric_count"], 1)
        self.assertFalse(payload["switch_state"])
        self.assertNotIn("qr_code", payload)

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
