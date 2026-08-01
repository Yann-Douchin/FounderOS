from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from founder_os.models import Event
from founder_os.ranking.memory import RankingMemory


NOW = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)


class RankingMemoryTests(unittest.TestCase):
    def test_newer_update_resurfaces_after_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            memory = RankingMemory(Path(folder) / "memory.json")
            original = Event(
                source="linear",
                id="linear:1",
                dedupe_key="linear:1",
                title="Décision initiale",
                occurred_at=NOW,
            )
            memory.acknowledge(original, NOW)
            self.assertTrue(memory.is_suppressed(original, NOW))
            updated = Event(
                source="linear",
                id="linear:1",
                dedupe_key="linear:1",
                title="Décision mise à jour",
                occurred_at=NOW + timedelta(minutes=1),
            )
            self.assertFalse(memory.is_suppressed(updated, NOW + timedelta(minutes=1)))

    def test_malformed_persisted_sections_fail_open_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "memory.json"
            path.write_text(
                json.dumps(
                    {
                        "displayed": {"bad": "not-an-object"},
                        "acknowledged": 42,
                        "snoozed_until": ["bad"],
                        "current_event_id": {"bad": True},
                    }
                ),
                encoding="utf-8",
            )
            memory = RankingMemory(path)
            event = Event(source="gmail", title="Réponse requise", occurred_at=NOW)
            self.assertFalse(memory.is_suppressed(event, NOW))
            memory.mark_displayed(event, NOW)
            self.assertTrue(path.is_file())

    def test_persisted_history_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            memory = RankingMemory(Path(folder) / "memory.json", max_entries=100)
            for index in range(110):
                event = Event(
                    source="gmail",
                    id=f"gmail:{index}",
                    title=f"Message {index}",
                    occurred_at=NOW + timedelta(seconds=index),
                )
                memory.mark_displayed(event, NOW + timedelta(seconds=index))
            self.assertLessEqual(len(memory.displayed), 100)


if __name__ == "__main__":
    unittest.main()
