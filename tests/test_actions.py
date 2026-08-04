from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from founder_os.actions import ActionOutbox, ActionOutboxConsumer
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

    def test_consumer_opens_once_and_writes_a_content_free_audit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            event = Event(
                source="linear",
                id="linear:safe",
                title="Client privé",
                url="https://linear.app/acme/issue/ABC-1",
                occurred_at=NOW,
            )
            queued = ActionOutbox(root).publish(event, "open")
            opened: list[str] = []
            consumer = ActionOutboxConsumer(
                root,
                browser_opener=lambda url: opened.append(url) is None,
            )
            results = consumer.consume_pending()
            second = consumer.consume_pending()
            audit = json.loads((root / "audit" / queued.name).read_text(encoding="utf-8"))
        self.assertEqual(opened, [event.url])
        self.assertEqual([result.outcome for result in results], ["opened"])
        self.assertEqual(second, [])
        self.assertEqual(audit["outcome"], "opened")
        self.assertNotIn("url", audit)
        self.assertNotIn(event.title, json.dumps(audit, ensure_ascii=False))

    def test_consumer_rejects_a_tampered_url_without_opening_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pending = root / "pending"
            pending.mkdir(parents=True)
            path = pending / "tampered.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "action": "open",
                        "event_id": "gmail:unsafe",
                        "source": "gmail",
                        "url": "file:///tmp/private",
                        "created_at": NOW.isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            opened: list[str] = []
            consumer = ActionOutboxConsumer(
                root,
                browser_opener=lambda url: opened.append(url) is None,
            )
            results = consumer.consume_pending()
            rejected = (root / "rejected" / "tampered.json").exists()
        self.assertEqual(opened, [])
        self.assertEqual([result.outcome for result in results], ["rejected_invalid"])
        self.assertTrue(rejected)

    def test_consumer_rejects_an_expired_open_action(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            event = Event(
                source="linear",
                id="linear:stale",
                title="Ancienne action",
                url="https://linear.app/acme/issue/OLD-1",
                occurred_at=NOW,
            )
            ActionOutbox(root).publish(
                event,
                "open",
                now=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
            opened: list[str] = []
            consumer = ActionOutboxConsumer(
                root,
                max_age_seconds=10,
                browser_opener=lambda url: opened.append(url) is None,
            )
            results = consumer.consume_pending()
        self.assertEqual(opened, [])
        self.assertEqual([result.outcome for result in results], ["rejected_invalid"])

    def test_consumer_rejects_valid_content_with_broader_file_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            event = Event(
                source="linear",
                id="linear:permissions",
                title="Permission test",
                url="https://linear.app/acme/issue/SEC-1",
                occurred_at=NOW,
            )
            queued = ActionOutbox(root).publish(event, "open")
            queued.chmod(0o640)
            opened: list[str] = []
            results = ActionOutboxConsumer(
                root,
                browser_opener=lambda url: opened.append(url) is None,
            ).consume_pending()
        self.assertEqual(opened, [])
        self.assertEqual([result.outcome for result in results], ["rejected_invalid"])

    def test_consumer_rejects_hard_linked_action_records(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            event = Event(
                source="linear",
                id="linear:linked",
                title="Hard-link test",
                url="https://linear.app/acme/issue/SEC-2",
                occurred_at=NOW,
            )
            queued = ActionOutbox(root).publish(event, "open")
            os.link(queued, queued.with_name("linked-copy.json"))
            opened: list[str] = []
            results = ActionOutboxConsumer(
                root,
                browser_opener=lambda url: opened.append(url) is None,
            ).consume_pending()
        self.assertEqual(opened, [])
        self.assertEqual([result.outcome for result in results], [
            "rejected_invalid",
            "rejected_invalid",
        ])

    def test_recovery_never_replays_an_indeterminate_open(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            processing = root / "processing"
            processing.mkdir(parents=True)
            path = processing / "claimed.json"
            path.write_text("{}\n", encoding="utf-8")
            opened: list[str] = []
            consumer = ActionOutboxConsumer(
                root,
                browser_opener=lambda url: opened.append(url) is None,
            )
            consumer.consume_pending()
            audit = json.loads((root / "audit" / path.name).read_text(encoding="utf-8"))
            rejected = (root / "rejected" / path.name).exists()
        self.assertEqual(opened, [])
        self.assertEqual(audit["outcome"], "indeterminate_after_restart")
        self.assertTrue(rejected)


if __name__ == "__main__":
    unittest.main()
