from __future__ import annotations

import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

from founder_os.models import Event, RankedEvent
from founder_os.ranking.llm import OpenAIResponsesTieBreaker


UTC = timezone.utc


class LLMFallbackTests(unittest.TestCase):
    def test_failed_attempts_still_respect_hourly_budget(self) -> None:
        fallback = OpenAIResponsesTieBreaker(
            {"model": "test-model", "timeout_seconds": 1, "max_calls_per_hour": 2},
            "test-key",
        )
        candidates = [
            RankedEvent(Event(id="event:1", source="linear", title="One", occurred_at=datetime.now(UTC)), 90, {}),
            RankedEvent(Event(id="event:2", source="gmail", title="Two", occurred_at=datetime.now(UTC)), 89, {}),
        ]
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")) as mocked:
            self.assertIsNone(fallback.choose(candidates))
            self.assertIsNone(fallback.choose(candidates))
            self.assertIsNone(fallback.choose(candidates))
        self.assertEqual(mocked.call_count, 2)


if __name__ == "__main__":
    unittest.main()
