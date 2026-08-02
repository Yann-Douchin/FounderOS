from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

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


class AlwaysConflictingDisplay(RecordingDisplay):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def draw(self, elements) -> None:
        self.attempts += 1
        raise DisplayConflict("firmware canvas blocker")


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

    def test_conflict_retries_are_exponential_and_a_new_decision_bypasses_the_delay(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(
                overrides={
                    "memory": {"path": str(Path(folder) / "memory.json")},
                    "display": {
                        "conflict_retry_seconds": 2,
                        "conflict_retry_max_seconds": 8,
                    },
                }
            )
            display = AlwaysConflictingDisplay()
            runtime = FounderOSRuntime(config, display=display)
            first = runtime.rank_engine.ranker.score(
                Event(source="linear", id="linear:first", title="Première décision"),
                NOW,
            )
            second = runtime.rank_engine.ranker.score(
                Event(source="gmail", id="gmail:second", title="Deuxième décision"),
                NOW,
            )
            try:
                with patch("founder_os.core.runtime.time.monotonic", side_effect=(10.0, 11.0, 12.1, 12.2)):
                    runtime._render(first, NOW)
                    skipped, error = runtime._render(first, NOW)
                    runtime._render(first, NOW)
                    runtime._render(second, NOW)
                self.assertFalse(skipped)
                self.assertIn("firmware canvas blocker", error)
                self.assertEqual(display.attempts, 3)
                self.assertEqual(runtime._display_retry_event_id, second.event.id)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
