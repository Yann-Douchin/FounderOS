from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from founder_os.models import Event


UTC = timezone.utc


class EventTests(unittest.TestCase):
    def test_normalizes_text_and_preserves_unicode(self) -> None:
        event = Event.from_mapping(
            {
                "source": " Calendar ",
                "title": "  Réunion   stratégie  ",
                "priority": 80,
                "extra_field": "décision",
            }
        )
        self.assertEqual(event.source, "calendar")
        self.assertEqual(event.title, "Réunion stratégie")
        self.assertEqual(event.metadata["extra_field"], "décision")
        self.assertTrue(event.id.startswith("calendar:"))

    def test_normalizes_decomposed_accents_to_nfc(self) -> None:
        event = Event(source="gmail", title="De\u0301cision d’inge\u0301nierie")
        self.assertEqual(event.title, "Décision d’ingénierie")

    def test_rejects_invalid_priority(self) -> None:
        with self.assertRaises(ValueError):
            Event(source="linear", title="Too high", priority=101)

    def test_expiry_is_explicit(self) -> None:
        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        event = Event(source="gmail", title="Question", expires_at=now + timedelta(minutes=1))
        self.assertFalse(event.is_expired(now))
        self.assertTrue(event.is_expired(now + timedelta(minutes=2)))


if __name__ == "__main__":
    unittest.main()
