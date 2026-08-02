from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from founder_os.config import load_config
from founder_os.core.runtime import FounderOSRuntime
from founder_os.display.busybar import RecordingDisplay
from founder_os.interaction import InputEvent
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
            self.assertTrue(display.frames[1])
            self.assertTrue(all(element["id"].startswith("icon-") for element in display.frames[1]))
            self.assertFalse(any(element.get("scroll_rate") for element in display.frames[1]))
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

    def test_refresh_probe_does_not_restart_scrolling_text(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(
                overrides={
                    "memory": {"path": str(Path(folder) / "memory.json")},
                    "display": {"content_icon": {"enabled": False}},
                }
            )
            display = RecordingDisplay()
            runtime = FounderOSRuntime(config, display=display)
            selected = RankedEvent(Event(source="gmail", title="Décision déjà validée"), 90, {})

            with patch("founder_os.core.runtime.time.monotonic", side_effect=(10.0, 26.0)):
                runtime._render(selected, NOW)
                runtime._render(selected, NOW)

            self.assertEqual(len(display.frames), 2)
            self.assertEqual(len(display.frames[1]), 1)
            self.assertFalse(display.frames[1][0].get("scroll_rate"))

    def test_same_event_content_update_is_sent_without_restarting_unchanged_elements(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(
                overrides={
                    "memory": {"path": str(Path(folder) / "memory.json")},
                    "display": {"content_icon": {"enabled": False}},
                }
            )
            display = RecordingDisplay()
            runtime = FounderOSRuntime(config, display=display)
            first = RankedEvent(
                Event(source="linear", title="Décision initiale", id="linear:decision"),
                90,
                {},
            )
            updated = RankedEvent(
                Event(source="linear", title="Décision corrigée", id="linear:decision"),
                90,
                {},
            )

            with patch("founder_os.core.runtime.time.monotonic", side_effect=(10.0, 10.1)):
                runtime._render(first, NOW)
                runtime._render(updated, NOW)

            self.assertEqual(len(display.frames), 2)
            self.assertEqual([element["id"] for element in display.frames[1]], ["title"])
            self.assertEqual(display.frames[1][0]["text"], "Décision corrigée")

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

    def test_trusted_normal_actions_acknowledge_snooze_and_open(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(
                overrides={
                    "memory": {"path": str(Path(folder) / "memory.json")},
                    "interaction": {"action_outbox_path": str(Path(folder) / "actions")},
                }
            )
            runtime = FounderOSRuntime(config, display=RecordingDisplay())
            try:
                event = Event(source="linear", id="linear:ack", title="À arbitrer", occurred_at=NOW)
                runtime.bus.publish(event)
                state = runtime.tick(NOW, force_poll=True)
                trusted = InputEvent(
                    key="ok",
                    event_id=state.selected.event.id,
                    trusted=True,
                    transport="signed_http",
                )
                self.assertEqual(runtime.handle_input(trusted), "acknowledge")
                self.assertFalse(any(item.id == event.id for item in runtime.bus.active(NOW)))

                snoozed = Event(source="gmail", id="gmail:snooze", title="Répondre", occurred_at=NOW)
                runtime.bus.publish(snoozed)
                state = runtime.tick(NOW)
                trusted = InputEvent(
                    key="back",
                    event_id=state.selected.event.id,
                    trusted=True,
                    transport="signed_http",
                )
                self.assertEqual(runtime.handle_input(trusted), "snooze")
                self.assertTrue(runtime.memory.is_suppressed(snoozed, NOW))

                linked = Event(
                    source="slack",
                    id="slack:open",
                    title="Ouvrir le fil",
                    url="https://example.test/thread",
                    occurred_at=NOW,
                )
                runtime.bus.publish(linked)
                state = runtime.tick(NOW)
                trusted = InputEvent(
                    key="custom",
                    event_id=state.selected.event.id,
                    trusted=True,
                    transport="signed_http",
                )
                self.assertEqual(runtime.handle_input(trusted), "open")
                pending = Path(folder) / "actions" / "pending"
                self.assertEqual(len(list(pending.glob("*.json"))), 1)
            finally:
                runtime.close()

    def test_layout_transition_clears_merged_hardware_elements(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(overrides={"memory": {"path": str(Path(folder) / "memory.json")}})
            display = RecordingDisplay()
            runtime = FounderOSRuntime(config, display=display)
            first = RankedEvent(Event(source="linear", title="Décision à prendre"), 90, {})
            second = RankedEvent(
                Event(source="claude", title="Autoriser Bash ?", kind="permission_request"),
                170,
                {},
            )
            try:
                with patch("founder_os.core.runtime.time.monotonic", side_effect=(10.1, 11.1)):
                    runtime._render(first, NOW)
                    runtime._render(second, NOW)
                operations = [operation[0] for operation in display.operations]
                self.assertEqual(operations, ["draw", "clear", "draw"])
            finally:
                runtime.close()

    def test_permission_bypasses_the_display_hold(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(overrides={"memory": {"path": str(Path(folder) / "memory.json")}})
            runtime = FounderOSRuntime(config, display=RecordingDisplay())
            blocker = RankedEvent(Event(source="linear", id="linear:block", title="Blocage"), 120, {})
            permission = RankedEvent(
                Event(source="chatgpt_codex", id="codex:permission", title="Autoriser ?", kind="permission_request"),
                1,
                {},
            )
            runtime._selected = blocker
            runtime._selected_since = 100
            runtime.bus.publish(blocker.event)
            try:
                with patch("founder_os.core.runtime.time.monotonic", return_value=101):
                    self.assertEqual(runtime._respect_hold(permission, NOW), permission)
            finally:
                runtime.close()

    def test_display_hold_applies_to_persistent_closure_events(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(overrides={"memory": {"path": str(Path(folder) / "memory.json")}})
            runtime = FounderOSRuntime(config, display=RecordingDisplay())
            first = RankedEvent(Event(source="closure", id="closure:first", title="First obligation"), 90, {})
            second = RankedEvent(Event(source="closure", id="closure:second", title="Second obligation"), 91, {})
            runtime._selected = first
            runtime._selected_since = 100
            try:
                with patch("founder_os.core.runtime.time.monotonic", return_value=101):
                    selected = runtime._respect_hold(
                        second,
                        NOW,
                        active_event_ids={first.event.id, second.event.id},
                    )
                self.assertEqual(selected, first)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
