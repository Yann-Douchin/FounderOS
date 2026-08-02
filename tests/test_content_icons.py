from __future__ import annotations

import unittest

from founder_os.display.content_icons import ICON_FRAMES, content_icon_frame, select_content_icon
from founder_os.models import Event


class ContentIconTests(unittest.TestCase):
    def test_semantics_take_priority_over_source(self) -> None:
        cases = (
            (Event(source="gmail", title="Production bloquée", kind="blocker"), "blocked"),
            (Event(source="linear", title="Réunion avec un investisseur"), "meeting"),
            (Event(source="linear", title="Répondre à l’e-mail du client"), "decision"),
            (Event(source="gmail", title="Mention Slack à traiter"), "waiting"),
            (Event(source="slack", title="Corriger le bug de l’API"), "waiting"),
            (Event(source="gmail", title="Croissance des ventes"), "waiting"),
            (Event(source="slack", title="Approuver la proposition"), "validation"),
            (Event(source="linear", title="Déploiement terminé avec succès"), "success"),
        )
        for event, expected in cases:
            with self.subTest(title=event.title):
                self.assertEqual(select_content_icon(event), expected)

    def test_source_and_action_fallbacks_cover_normalized_events(self) -> None:
        cases = (
            (Event(source="calendar", title="Point quotidien"), "meeting"),
            (Event(source="gmail", title="Nouveau sujet"), "waiting"),
            (Event(source="slack", title="Nouveau sujet"), "waiting"),
            (Event(source="linear", title="Nouveau sujet"), "waiting"),
            (Event(source="demo", title="Nouveau sujet", action_required=True), "decision"),
            (Event(source="demo", title="Nouveau sujet"), "waiting"),
        )
        for event, expected in cases:
            with self.subTest(source=event.source, action_required=event.action_required):
                self.assertEqual(select_content_icon(event), expected)

    def test_every_icon_has_two_distinct_valid_frames(self) -> None:
        self.assertEqual(set(ICON_FRAMES), {"waiting", "blocked", "decision", "meeting", "validation", "success"})
        for icon_name, frames in ICON_FRAMES.items():
            with self.subTest(icon=icon_name):
                self.assertEqual(len(frames), 2)
                self.assertNotEqual(frames[0], frames[1])
                self.assertTrue(all(len(frame) == 8 for frame in frames))
                self.assertTrue(all(len(row) == 8 for frame in frames for row in frame))

    def test_frame_index_loops(self) -> None:
        event = Event(source="linear", title="Tâche suivante")
        first_name, first_frame = content_icon_frame(event, 0)
        third_name, third_frame = content_icon_frame(event, 2)
        self.assertEqual(first_name, "waiting")
        self.assertEqual(third_name, first_name)
        self.assertEqual(third_frame, first_frame)


if __name__ == "__main__":
    unittest.main()
