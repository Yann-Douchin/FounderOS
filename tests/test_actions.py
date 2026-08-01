from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from founder_os.actions import ActionOutbox
from founder_os.models import Event


NOW = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)


class ActionOutboxTests(unittest.TestCase):
    def test_only_safe_web_urls_are_queued(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            outbox = ActionOutbox(folder)
            for index, unsafe_url in enumerate(
                (
                    "javascript:alert(1)",
                    "file:///tmp/private",
                    "http://example.test/insecure",
                    "https://user:password@example.test/private",
                )
            ):
                event = Event(
                    source="slack",
                    id=f"slack:unsafe:{index}",
                    title="Lien non fiable",
                    url=unsafe_url,
                    occurred_at=NOW,
                )
                self.assertIsNone(outbox.publish(event, "open", now=NOW))
            safe = Event(
                source="linear",
                id="linear:safe",
                title="Ouvrir Linear",
                url="https://linear.app/acme/issue/ABC-1",
                occurred_at=NOW,
            )
            path = outbox.publish(safe, "open", now=NOW)
            self.assertIsNotNone(path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["url"], safe.url)

    def test_pending_queue_has_a_hard_limit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            outbox = ActionOutbox(folder, max_pending=1)
            first = Event(
                source="linear",
                id="linear:1",
                title="Premier lien",
                url="https://linear.app/one",
                occurred_at=NOW,
            )
            second = Event(
                source="linear",
                id="linear:2",
                title="Second lien",
                url="https://linear.app/two",
                occurred_at=NOW,
            )
            self.assertIsNotNone(outbox.publish(first, "open", now=NOW))
            self.assertIsNone(outbox.publish(second, "open", now=NOW))


if __name__ == "__main__":
    unittest.main()
