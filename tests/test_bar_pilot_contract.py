from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BarPilotContractTests(unittest.TestCase):
    def test_reference_is_pinned_and_all_53_routes_are_governed(self) -> None:
        contract = json.loads(
            (ROOT / "tests" / "fixtures" / "barpilot-api25-contract.json").read_text(encoding="utf-8")
        )
        reference = contract["reference"]
        self.assertEqual(reference["commit"], "5c4afe96e178982d7e5f95a9dfea0cf761804d80")
        self.assertRegex(reference["sha256"], r"^[0-9a-f]{64}$")
        endpoints = contract["barpilot_endpoints"]
        self.assertEqual(len(endpoints), 53)
        self.assertEqual(len({endpoint["path"] for endpoint in endpoints}), 53)
        self.assertEqual(sum(len(endpoint["methods"]) for endpoint in endpoints), 69)
        self.assertTrue(all(endpoint["support"] in {"emulated", "stateful"} for endpoint in endpoints))
        self.assertTrue(all(endpoint["behavior"] for endpoint in endpoints))
        self.assertEqual(contract["status_websocket"]["path"], "/api/status/ws")

    def test_firmware_quirks_cover_every_governed_blocker(self) -> None:
        contract = json.loads(
            (ROOT / "tests" / "fixtures" / "barpilot-api25-contract.json").read_text(encoding="utf-8")
        )
        quirks = set(contract["firmware_1_1_1_quirks"])
        self.assertIn("busy_timer_blocks_canvas_at_all_priorities", quirks)
        self.assertIn("physical_sessions_are_absent_from_busy_snapshot", quirks)
        self.assertIn("device_menu_blocks_canvas", quirks)
        self.assertIn("smart_home_switch_timer_blocks_canvas", quirks)

    def test_browser_animation_clock_uses_a_worker_and_absolute_scroll_time(self) -> None:
        clock = (ROOT / "web" / "src" / "lib" / "background-clock.js").read_text(encoding="utf-8")
        renderer = (ROOT / "web" / "src" / "lib" / "renderer.js").read_text(encoding="utf-8")
        self.assertIn("new Worker", clock)
        self.assertIn("createBackgroundClock", renderer)
        self.assertIn("const phase = elapsed % (moving + repeatDelay)", renderer)


if __name__ == "__main__":
    unittest.main()
