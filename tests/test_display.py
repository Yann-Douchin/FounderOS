from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from founder_os.display.busybar import BusyBarDisplay
from founder_os.display.layouts import event_layout, idle_layout
from founder_os.models import Event, RankedEvent


UTC = timezone.utc
NOW = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size=-1):
        return b'{"result":"OK"}'


class DisplayTests(unittest.TestCase):
    def test_layout_stays_inside_72_by_16(self) -> None:
        ranked = RankedEvent(
            Event(source="linear", title="Quantity Fix blocked", priority=90, kind="blocker"),
            117,
            {},
        )
        for frame in (event_layout(ranked, NOW), idle_layout()):
            for element in frame:
                self.assertGreaterEqual(element.get("x", 0), 0)
                self.assertGreaterEqual(element.get("y", 0), 0)
                self.assertLessEqual(element.get("x", 0), 72)
                self.assertLessEqual(element.get("y", 0), 16)
                if element["type"] == "rectangle":
                    self.assertLessEqual(element["x"] + element["width"], 72)
                    self.assertLessEqual(element["y"] + element["height"], 16)

    def test_long_title_is_complete_in_first_frame(self) -> None:
        event = Event(source="linear", title="QTY-142 Quantity Fix blocked", priority=90, kind="blocker")
        frame = event_layout(RankedEvent(event, 117, {}), NOW)
        title_parts = [element["text"] for element in frame if element["id"].startswith("title-")]
        self.assertEqual(" ".join(title_parts), event.title)
        self.assertNotIn("scroll_rate", next(element for element in frame if element["id"] == "title-1"))

    def test_accented_title_uses_hardware_global_font_without_text_loss(self) -> None:
        title = "Escalader 18 décisions d’ingénierie bloquantes"
        event = Event(source="gmail", title=title, priority=90, action_required=True)
        frame = event_layout(RankedEvent(event, 117, {}), NOW)
        title_element = next(element for element in frame if element["id"] == "title")
        self.assertEqual(title_element["text"], title)
        self.assertEqual(title_element["font"], "global")
        self.assertEqual(title_element["x"], 16)
        self.assertEqual(title_element["y"], 5)
        self.assertEqual(title_element["width"], 56)
        self.assertGreater(title_element["scroll_rate"], 0)

    def test_animated_icon_uses_stable_pixel_ids_within_api_limit(self) -> None:
        event = Event(source="gmail", title="Décision bloquée", kind="blocker")
        ranked = RankedEvent(event, 117, {})
        first = event_layout(ranked, NOW, icon_frame=0)
        second = event_layout(ranked, NOW, icon_frame=1)
        first_pixels = [element for element in first if element["id"].startswith("icon-")]
        second_pixels = [element for element in second if element["id"].startswith("icon-")]

        self.assertLessEqual(len(first), 100)
        self.assertEqual(len(first_pixels), 64)
        self.assertEqual([pixel["id"] for pixel in first_pixels], [pixel["id"] for pixel in second_pixels])
        self.assertNotEqual(
            [pixel["fill_colors"] for pixel in first_pixels],
            [pixel["fill_colors"] for pixel in second_pixels],
        )
        self.assertTrue(all(pixel["width"] == pixel["height"] == 1 for pixel in first_pixels))

    def test_content_icon_can_be_disabled_without_changing_the_title(self) -> None:
        event = Event(source="gmail", title="Décision à prendre", action_required=True)
        frame = event_layout(RankedEvent(event, 90, {}), NOW, icon_frame=None)
        self.assertFalse(any(element["id"].startswith("icon-") for element in frame))
        title_element = next(element for element in frame if element["id"] == "title")
        self.assertEqual(title_element["text"], event.title)
        self.assertEqual(title_element["x"], 6)
        self.assertEqual(title_element["width"], 66)

    def test_global_atlas_contains_french_glyphs(self) -> None:
        atlas = json.loads((ROOT / "public/fonts/font-atlas.json").read_text(encoding="utf-8"))
        required = "ÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸŒàâäçéèêëîïôöùûüÿœ’«»"
        missing = [character for character in required if str(ord(character)) not in atlas["global"]["glyphs"]]
        self.assertEqual(missing, [])

    def test_french_accents_are_inside_the_visible_global_font_line(self) -> None:
        atlas = json.loads((ROOT / "public/fonts/font-atlas.json").read_text(encoding="utf-8"))
        glyphs = atlas["global"]["glyphs"]
        self.assertTrue(all(glyph["oy"] >= 0 for glyph in glyphs.values()))
        self.assertNotEqual(glyphs[str(ord("É"))]["rows"], glyphs[str(ord("E"))]["rows"])

    def test_due_time_wins_over_generic_action_label(self) -> None:
        event = Event(
            source="calendar",
            title="Investor update",
            priority=90,
            action_required=True,
            due_at=datetime(2026, 8, 1, 10, 7, tzinfo=UTC),
        )
        frame = event_layout(RankedEvent(event, 110, {}), NOW)
        status = next(element for element in frame if element["id"] == "status")
        self.assertEqual(status["text"], "7M")

    def test_permission_request_has_explicit_allow_and_deny_affordances(self) -> None:
        event = Event(
            source="claude",
            title="Run npm test?",
            kind="permission_request",
            action_required=True,
            expires_at=datetime(2026, 8, 1, 10, 0, 45, tzinfo=UTC),
        )
        frame = event_layout(RankedEvent(event, 170, {}), NOW, icon_frame=0)
        by_id = {element["id"]: element for element in frame}
        self.assertEqual(by_id["source"]["text"], "CLAUDE")
        self.assertEqual(by_id["status"]["text"], "? 45S")
        self.assertEqual(by_id["deny"]["text"], "NON")
        self.assertEqual(by_id["allow"]["text"], "OUI")
        self.assertLessEqual(len(frame), 100)

    def test_accented_permission_keeps_allow_and_deny_affordances(self) -> None:
        event = Event(
            source="chatgpt_codex",
            title="Déployer la version corrigée ?",
            kind="permission_request",
            action_required=True,
            expires_at=datetime(2026, 8, 1, 10, 0, 45, tzinfo=UTC),
        )
        frame = event_layout(RankedEvent(event, 170, {}), NOW, icon_frame=0)
        by_id = {element["id"]: element for element in frame}
        self.assertEqual(by_id["deny"]["text"], "NON")
        self.assertEqual(by_id["allow"]["text"], "OUI")
        self.assertEqual(by_id["title"]["text"], "Déployer la version corrigée ?")

    def test_agent_usage_renders_two_quota_bars(self) -> None:
        event = Event(
            source="chatgpt_codex",
            title="Utilisation ChatGPT / Codex",
            kind="agent_usage",
            metadata={
                "windows": [
                    {"label": "5H", "used_percent": 25},
                    {"label": "SEM", "used_percent": 90},
                ]
            },
        )
        frame = event_layout(RankedEvent(event, 14, {}), NOW)
        by_id = {element["id"]: element for element in frame}
        self.assertEqual(by_id["source"]["text"], "CODEX")
        self.assertEqual(by_id["window-0"]["text"], "5H")
        self.assertEqual(by_id["window-1"]["text"], "SEM")
        self.assertEqual(by_id["used-0"]["width"], 12)
        self.assertEqual(by_id["used-1"]["width"], 45)

    def test_uses_exact_busybar_draw_contract(self) -> None:
        calls = []

        def capture(request, timeout):
            calls.append(request)
            return FakeResponse()

        display = BusyBarDisplay(
            "127.0.0.1:8080",
            application_name="founderos",
            priority=90,
            api_token="device-secret",
            api_semver="25.0.0",
        )
        with patch("founder_os.display.busybar._OPENER.open", side_effect=capture):
            display.draw([{"id": "t", "type": "text", "text": "HI", "x": 0, "y": 0}])
            display.clear()
        body = json.loads(calls[0].data.decode("utf-8"))
        self.assertEqual(calls[0].get_method(), "POST")
        self.assertEqual(calls[0].full_url, "http://127.0.0.1:8080/api/display/draw")
        self.assertEqual(body["application_name"], "founderos")
        self.assertEqual(body["priority"], 90)
        self.assertEqual(calls[0].get_header("X-api-token"), "device-secret")
        self.assertEqual(calls[0].get_header("X-api-sem-ver"), "25.0.0")
        self.assertEqual(calls[1].get_method(), "DELETE")
        self.assertEqual(
            calls[1].full_url,
            "http://127.0.0.1:8080/api/display/draw?application_name=founderos",
        )


if __name__ == "__main__":
    unittest.main()
