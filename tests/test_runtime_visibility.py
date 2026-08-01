from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from founder_os.config import load_config
from founder_os.core.runtime import FounderOSRuntime
from founder_os.display.busybar import DisplayConflict, RecordingDisplay
from founder_os.interaction import InputEvent
from founder_os.models import Event


NOW = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)


class ConflictingDisplay(RecordingDisplay):
    def draw(self, elements) -> None:
        if self.frames:
            raise DisplayConflict("higher-priority owner")
        super().draw(elements)


class RuntimeVisibilityTests(unittest.TestCase):
    def test_input_is_disabled_when_the_selected_event_is_not_visible(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(
                overrides={
                    "memory": {"path": str(Path(folder) / "memory.json")},
                }
            )
            runtime = FounderOSRuntime(config, display=ConflictingDisplay())
            try:
                event = Event(source="linear", id="linear:visible", title="Décision visible", occurred_at=NOW)
                runtime.bus.publish(event)
                state = runtime.tick(NOW, force_poll=True)
                self.assertEqual(runtime._input_context()["event_id"], event.id)
                runtime._last_draw_at = 0
                second_state = runtime.tick(NOW)
                self.assertIn("higher-priority owner", second_state.display_error)
                self.assertEqual(runtime._input_context()["event_id"], "")
                trusted = InputEvent(
                    key="ok",
                    event_id=state.selected.event.id,
                    trusted=True,
                    transport="signed_http",
                )
                self.assertIsNone(runtime.handle_input(trusted))
                self.assertFalse(runtime.memory.is_suppressed(event, NOW))
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
