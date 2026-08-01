from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from founder_os.connectors.base import ConnectorStaleError
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
        connector = LinearConnector.__new__(LinearConnector)
        connector.timezone = "Europe/Madrid"
        connector.active_event_ttl = timedelta(hours=48)
        event = connector._normalize(
            {
                "id": "issue-1",
                "identifier": "QTY-142",
                "title": "Quantity Fix blocked",
                "priority": 1,
                "dueDate": "2026-08-01",
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
        self.assertEqual(event.due_at, datetime(2026, 8, 1, 21, 59, 59, 999999, tzinfo=UTC))
        self.assertGreater(event.expires_at, event.due_at)

    def test_linear_unblocked_state_is_not_a_blocker(self) -> None:
        issue = {
            "state": {"name": "Unblocked", "type": "started"},
            "labels": {"nodes": [{"name": "Ready"}]},
        }
        self.assertFalse(LinearConnector._is_blocked(issue))

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

    def test_slack_dependency_signal_surfaces_without_a_founder_mention(self) -> None:
        connector = SlackConnector.__new__(SlackConnector)
        connector.channel_names = {"C1": "launch"}
        connector.mention_markers = ["<@founder>"]
        connector.urgent_keywords = ["blocked"]
        message = {"ts": "1785578100.000000", "text": "Waiting for merchant access"}
        event = connector._normalize("C1", message, NOW)
        self.assertIsNotNone(event)
        self.assertEqual(event.kind, "waiting")
        self.assertEqual(event.metadata["signal"], "dependency")

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

    def test_gmail_does_not_treat_an_important_invoice_as_an_action(self) -> None:
        connector = GmailConnector.__new__(GmailConnector)
        connector.vip_senders = set()
        event = connector._normalize(
            self._gmail_message("invoice", "Facture et reçu de paiement", important=True),
            NOW,
        )
        self.assertFalse(event.action_required)
        self.assertEqual(event.metadata["classification"], "fyi")
        self.assertEqual(event.priority, 34)

    def test_gmail_respects_explicit_non_action_language(self) -> None:
        connector = GmailConnector.__new__(GmailConnector)
        connector.vip_senders = set()
        event = connector._normalize(
            self._gmail_message("fyi", "Décision publiée, aucune action requise", important=True),
            NOW,
        )
        self.assertFalse(event.action_required)
        self.assertEqual(event.metadata["classification"], "fyi")

    def test_gmail_vip_matching_rejects_address_substrings(self) -> None:
        connector = GmailConnector.__new__(GmailConnector)
        connector.vip_senders = {"ceo@example.test"}
        self.assertTrue(connector._is_vip_sender("ceo@example.test"))
        self.assertFalse(connector._is_vip_sender("not-ceo@example.test"))

    @staticmethod
    def _gmail_message(message_id: str, subject: str, *, important: bool = False) -> dict:
        labels = ["UNREAD", "IMPORTANT"] if important else ["UNREAD"]
        return {
            "id": message_id,
            "internalDate": "1785578100000",
            "labelIds": labels,
            "payload": {"headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "billing@example.test"},
            ]},
        }

    def test_gmail_detects_nested_attachment_metadata(self) -> None:
        payload = {"parts": [{"parts": [{"filename": "pièce-comptable.pdf"}]}]}
        self.assertTrue(GmailConnector._has_attachment(payload))

    def test_calendar_near_term_event(self) -> None:
        connector = GoogleCalendarConnector.__new__(GoogleCalendarConnector)
        connector.timezone = "Europe/Madrid"
        event = connector._normalize(
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
        self.assertEqual(event.title, "PRÉPA Comité stratégie")
        self.assertEqual(event.urgency, "high")
        self.assertTrue(event.metadata["readiness"])

    def test_calendar_all_day_event_is_not_dropped_or_marked_overdue(self) -> None:
        connector = GoogleCalendarConnector.__new__(GoogleCalendarConnector)
        connector.timezone = "Europe/Madrid"
        event = connector._normalize(
            {
                "id": "cal-all-day",
                "summary": "Journée stratégie",
                "status": "confirmed",
                "updated": "2026-08-01T09:00:00Z",
                "start": {"date": "2026-08-01"},
                "end": {"date": "2026-08-02"},
            },
            NOW,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.kind, "calendar_all_day")
        self.assertIsNone(event.due_at)
        self.assertEqual(event.expires_at, datetime(2026, 8, 1, 22, 0, tzinfo=UTC))
        self.assertTrue(event.metadata["all_day"])

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
            with self.assertRaises(ConnectorStaleError):
                connector.poll(NOW)

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
