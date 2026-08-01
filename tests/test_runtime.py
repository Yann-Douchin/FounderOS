from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from founder_os.config import load_config
from founder_os.core.runtime import FounderOSRuntime
from founder_os.display.busybar import RecordingDisplay
from founder_os.models import Event, RankedEvent


UTC = timezone.utc
NOW = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


class RuntimeTests(unittest.TestCase):
    def test_demo_selects_one_event_and_one_frame(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(
                overrides={
                    "memory": {"path": str(Path(folder) / "memory.json")},
                    "connectors": {"demo": {"enabled": True, "scenario": "mixed"}},
                }
            )
            display = RecordingDisplay()
            runtime = FounderOSRuntime(config, display=display)
            state = runtime.tick(force_poll=True)
            self.assertEqual(state.event_count, 4)
            self.assertEqual(state.selected.event.source, "linear")
            self.assertEqual(len(display.frames), 1)
            title_elements = [element for element in display.frames[0] if element["id"].startswith("title")]
            self.assertGreaterEqual(len(title_elements), 1)

    def test_icon_phase_forces_a_new_frame_without_changing_the_decision(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(
                overrides={
                    "memory": {"path": str(Path(folder) / "memory.json")},
                    "display": {"content_icon": {"frame_seconds": 1.0}},
                }
            )
            display = RecordingDisplay()
            runtime = FounderOSRuntime(config, display=display)
            selected = RankedEvent(
                Event(source="linear", title="Corriger le bug API", id="linear:bug"),
                100,
                {},
            )

            with patch("founder_os.core.runtime.time.monotonic", side_effect=(10.1, 11.1)):
                first_displayed, _ = runtime._render(selected, NOW)
                second_displayed, _ = runtime._render(selected, NOW)

            self.assertTrue(first_displayed)
            self.assertTrue(second_displayed)
            self.assertEqual(len(display.frames), 2)
            first_pixels = [
                element["fill_colors"]
                for element in display.frames[0]
                if element["id"].startswith("icon-")
            ]
            second_pixels = [
                element["fill_colors"]
                for element in display.frames[1]
                if element["id"].startswith("icon-")
            ]
            self.assertNotEqual(first_pixels, second_pixels)

    def test_disabling_icon_avoids_animation_only_redraws(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(
                overrides={
                    "memory": {"path": str(Path(folder) / "memory.json")},
                    "display": {"content_icon": {"enabled": False}},
                }
            )
            display = RecordingDisplay()
            runtime = FounderOSRuntime(config, display=display)
            selected = RankedEvent(Event(source="linear", title="Tâche active"), 90, {})

            with patch("founder_os.core.runtime.time.monotonic", side_effect=(10.1, 11.1)):
                first_displayed, _ = runtime._render(selected, NOW)
                second_displayed, _ = runtime._render(selected, NOW)

            self.assertTrue(first_displayed)
            self.assertFalse(second_displayed)
            self.assertEqual(len(display.frames), 1)
            self.assertFalse(
                any(element["id"].startswith("icon-") for element in display.frames[0])
            )


if __name__ == "__main__":
    unittest.main()
