from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from founder_os.connectors.calendar import GoogleCalendarConnector
from founder_os.connectors.feed import JsonlInboxConnector
from founder_os.connectors.gmail import GmailConnector
from founder_os.connectors.linear import LinearConnector
from founder_os.connectors.registry import build_connectors
from founder_os.connectors.snapshot import JsonSnapshotConnector
from founder_os.connectors.slack import SlackConnector


UTC = timezone.utc
NOW = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


class ConnectorNormalizationTests(unittest.TestCase):
    def test_linear_blocker(self) -> None:
        event = LinearConnector._normalize(
            {
                "id": "issue-1",
                "identifier": "QTY-142",
                "title": "Quantity Fix blocked",
                "priority": 1,
                "updatedAt": "2026-08-01T09:55:00Z",
                "state": {"name": "Blocked", "type": "started"},
                "team": {"key": "QTY"},
                "labels": {"nodes": [{"name": "blocker"}]},
            },
            NOW,
        )
        self.assertEqual(event.kind, "blocker")
        self.assertEqual(event.urgency, "critical")
        self.assertTrue(event.action_required)

    def test_slack_mention(self) -> None:
        connector = SlackConnector.__new__(SlackConnector)
        connector.channel_names = {"C1": "launch"}
        connector.mention_markers = ["<@u-founder>"]
        connector.urgent_keywords = ["blocked", "approval"]
        event = connector._normalize(
            "C1",
            {"ts": "1785578100.000000", "text": "<@U-FOUNDER> approval needed", "user": "U2"},
            NOW,
        )
        self.assertIsNotNone(event)
        self.assertTrue(event.action_required)
        self.assertEqual(event.kind, "blocker")

    def test_gmail_preserves_accented_sender(self) -> None:
        connector = GmailConnector.__new__(GmailConnector)
        connector.vip_senders = {"investisseur.fr"}
        event = connector._normalize(
            {
                "id": "m1",
                "threadId": "t1",
                "internalDate": "1785578100000",
                "labelIds": ["UNREAD", "IMPORTANT"],
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Décision requise"},
                        {"name": "From", "value": "Élodie <elodie@investisseur.fr>"},
                    ]
                },
            },
            NOW,
        )
        self.assertIn("Élodie", event.title)
        self.assertIn("Décision", event.title)
        self.assertEqual(event.urgency, "high")

    def test_calendar_near_term_event(self) -> None:
        event = GoogleCalendarConnector._normalize(
            {
                "id": "cal-1",
                "summary": "Comité stratégie",
                "status": "confirmed",
                "updated": "2026-08-01T09:00:00Z",
                "start": {"dateTime": "2026-08-01T10:07:00Z"},
                "end": {"dateTime": "2026-08-01T10:37:00Z"},
                "attendees": [{"self": True, "responseStatus": "accepted"}],
            },
            NOW,
        )
        self.assertEqual(event.title, "Comité stratégie")
        self.assertEqual(event.urgency, "high")

    def test_jsonl_inbox_waits_for_complete_line(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            path.write_bytes(b'{"title":"R\xc3\xa9ponse pr\xc3\xaate","priority":70')
            connector = JsonlInboxConnector(
                {"inbox_path": str(path), "poll_interval_seconds": 1}, source="chatgpt_codex"
            )
            self.assertEqual(connector.poll(NOW), [])
            with path.open("ab") as stream:
                stream.write(b'}\n')
            events = connector.poll(NOW)
            self.assertEqual(events[0].title, "Réponse prête")

    def test_snapshot_connector_preserves_accents_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "gmail.json"
            path.write_text(
                """{
  "schema_version": 1,
  "source": "gmail",
  "generated_at": "2026-08-01T09:55:00Z",
  "events": [
    {
      "id": "gmail:décision",
      "title": "Arbitrer la clause résiduelle",
      "priority": 91,
      "action_required": true
    }
  ]
}
""",
                encoding="utf-8",
            )
            connector = JsonSnapshotConnector(
                {
                    "snapshot_path": str(path),
                    "max_snapshot_age_minutes": 120,
                    "poll_interval_seconds": 1,
                },
                source="gmail",
            )
            events = connector.poll(NOW)
            self.assertEqual(events[0].source, "gmail")
            self.assertEqual(events[0].title, "Arbitrer la clause résiduelle")
            self.assertGreater(events[0].expires_at, NOW)

    def test_stale_snapshot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "slack.json"
            path.write_text(
                '{"source":"slack","generated_at":"2026-07-30T00:00:00Z","events":'
                '[{"title":"Ancienne mention"}]}',
                encoding="utf-8",
            )
            connector = JsonSnapshotConnector(
                {
                    "snapshot_path": str(path),
                    "max_snapshot_age_minutes": 60,
                    "poll_interval_seconds": 1,
                },
                source="slack",
            )
            self.assertEqual(connector.poll(NOW), [])

    def test_registry_builds_snapshot_without_api_secret(self) -> None:
        connectors = build_connectors(
            {
                "linear": {
                    "enabled": True,
                    "mode": "snapshot",
                    "snapshot_path": ".data/connectors/linear.json",
                }
            }
        )
        self.assertEqual(len(connectors), 1)
        self.assertIsInstance(connectors[0], JsonSnapshotConnector)
        self.assertEqual(connectors[0].name, "linear")


if __name__ == "__main__":
    unittest.main()
