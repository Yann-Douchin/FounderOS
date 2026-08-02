from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from founder_os.connectors.base import ConnectorError
from founder_os.connectors.calendar import GoogleCalendarConnector
from founder_os.connectors.gmail import GmailConnector
from founder_os.connectors.linear import LinearConnector
from founder_os.connectors.slack import SlackConnector


NOW = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)


class DirectConnectorPollingTests(unittest.TestCase):
    def test_linear_poll_uses_personal_api_key_auth_and_normalizes_live_shape(self) -> None:
        payload = {
            "data": {
                "viewer": {
                    "assignedIssues": {
                        "nodes": [
                            {
                                "id": "issue-1",
                                "identifier": "BUSY-1",
                                "title": "Valider les accents",
                                "priority": 1,
                                "updatedAt": NOW.isoformat(),
                                "state": {"name": "Started", "type": "started"},
                                "team": {"key": "BUSY"},
                                "labels": {"nodes": []},
                                "url": "https://linear.app/acme/issue/BUSY-1",
                            }
                        ]
                    }
                }
            }
        }
        with patch.dict(os.environ, {"TEST_LINEAR_TOKEN": "linear-token"}), patch(
            "founder_os.connectors.linear.request_json",
            return_value=payload,
        ) as request:
            connector = LinearConnector(
                {"token_env": "TEST_LINEAR_TOKEN", "team_keys": ["BUSY"]}
            )
            events = connector.poll(NOW)
        self.assertEqual(events[0].title, "BUSY-1 Valider les accents")
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "linear-token")

    def test_linear_portfolio_paginates_filters_and_rolls_up_project_risk(self) -> None:
        def issue(issue_id: str, priority: int, blocked: bool = False) -> dict:
            state = "Blocked" if blocked else "Started"
            return self._linear_issue(issue_id, priority, state)

        pages = (
            {"data": {"viewer": {"id": "founder"}, "issues": {
                "nodes": [issue("1", 1), issue("noise", 4)],
                "pageInfo": {"hasNextPage": True, "endCursor": "next"},
            }}},
            {"data": {"viewer": {"id": "founder"}, "issues": {
                "nodes": [issue("2", 3, blocked=True)],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}},
        )
        with patch.dict(os.environ, {"TEST_LINEAR_TOKEN": "oauth-token"}), patch(
            "founder_os.connectors.linear.request_json", side_effect=pages
        ) as request:
            connector = LinearConnector({
                "token_env": "TEST_LINEAR_TOKEN",
                "auth_scheme": "bearer",
                "scope": "portfolio",
                "team_keys": ["BUSY"],
                "page_size": 2,
                "max_issues": 4,
            })
            events = connector.poll(NOW)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].metadata["rollup"])
        self.assertEqual(events[0].metadata["issue_count"], 2)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer oauth-token")
        self.assertEqual(request.call_args_list[1].kwargs["body"]["variables"]["after"], "next")

    @staticmethod
    def _linear_issue(issue_id: str, priority: int, state: str) -> dict:
        return {
            "id": issue_id,
            "identifier": f"BUSY-{issue_id}",
            "title": f"Risque {issue_id}",
            "priority": priority,
            "updatedAt": NOW.isoformat(),
            "state": {"name": state, "type": "started"},
            "team": {"key": "BUSY"},
            "assignee": {"id": "teammate", "name": "Alex"},
            "project": {"id": "launch", "name": "Lancement", "url": "https://linear.app/project/launch"},
            "labels": {"nodes": []},
            "url": f"https://linear.app/issue/{issue_id}",
        }

    def test_slack_poll_follows_bounded_cursor_pagination(self) -> None:
        pages = (
            {
                "ok": True,
                "messages": [{"ts": "1785578400.000001", "text": "approval requise"}],
                "response_metadata": {"next_cursor": "next"},
            },
            {
                "ok": True,
                "messages": [{"ts": "1785578401.000002", "text": "blocked en production"}],
                "response_metadata": {"next_cursor": ""},
            },
        )
        with patch.dict(os.environ, {"TEST_SLACK_TOKEN": "slack-token"}), patch(
            "founder_os.connectors.slack.request_json",
            side_effect=pages,
        ) as request:
            connector = SlackConnector(
                {
                    "token_env": "TEST_SLACK_TOKEN",
                    "channel_ids": ["C123"],
                    "urgent_keywords": ["blocked", "approval"],
                    "max_pages": 3,
                    "workspace_url": "https://acme.slack.com",
                }
            )
            events = connector.poll(NOW)
        self.assertEqual(len(events), 2)
        self.assertEqual(request.call_count, 2)
        self.assertIn("/archives/C123/", events[0].url)

    def test_slack_remote_error_text_is_not_repeated(self) -> None:
        with patch.dict(os.environ, {"TEST_SLACK_TOKEN": "slack-token"}), patch(
            "founder_os.connectors.slack.request_json",
            return_value={"ok": False, "error": "private subject from remote service"},
        ):
            connector = SlackConnector({"token_env": "TEST_SLACK_TOKEN", "channel_ids": ["C123"]})
            with self.assertRaisesRegex(ConnectorError, "unknown_error") as raised:
                connector.poll(NOW)
        self.assertNotIn("private subject", str(raised.exception))

    def test_slack_poll_reads_actionable_thread_replies(self) -> None:
        responses = (
            {
                "ok": True,
                "messages": [{"ts": "1785578400.000001", "text": "Launch update", "reply_count": 1, "user": "UOTHER"}],
                "response_metadata": {"next_cursor": ""},
            },
            {
                "ok": True,
                "messages": [
                    {"ts": "1785578400.000001", "text": "Launch update", "user": "UOTHER"},
                    {"ts": "1785578401.000002", "thread_ts": "1785578400.000001", "text": "I will send the proof", "user": "USELF"},
                ],
                "response_metadata": {"next_cursor": ""},
            },
        )
        with patch.dict(os.environ, {"TEST_SLACK_TOKEN": "slack-token"}), patch(
            "founder_os.connectors.slack.request_json", side_effect=responses
        ) as request:
            events = SlackConnector({
                "token_env": "TEST_SLACK_TOKEN",
                "channel_ids": ["C123"],
                "self_user_ids": ["USELF"],
                "user_names": {"USELF": "Yann"},
            }).poll(NOW)
        reply = next(event for event in events if event.metadata.get("thread_ts"))
        self.assertEqual(reply.metadata["direction"], "outgoing")
        self.assertEqual(reply.metadata["sender_name"], "Yann")
        self.assertIn("conversations.replies", request.call_args_list[1].args[0])

    def test_linear_remote_graphql_message_is_not_repeated(self) -> None:
        with patch.dict(os.environ, {"TEST_LINEAR_TOKEN": "linear-token"}), patch(
            "founder_os.connectors.linear.request_json",
            return_value={"errors": [{"message": "private issue title", "extensions": {"code": "BAD USER DATA"}}]},
        ):
            connector = LinearConnector({"token_env": "TEST_LINEAR_TOKEN", "team_keys": ["BUSY"]})
            with self.assertRaisesRegex(ConnectorError, "graphql_error") as raised:
                connector.poll(NOW)
        self.assertNotIn("private issue title", str(raised.exception))

    def test_gmail_poll_fetches_metadata_with_static_access_token(self) -> None:
        def response(url, **kwargs):
            if url.endswith("/messages"):
                return {"messages": [{"id": "m1"}]}
            return {
                "id": "m1",
                "threadId": "t1",
                "internalDate": "1785578400000",
                "labelIds": ["UNREAD"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Élodie <elodie@example.test>"},
                        {"name": "Subject", "value": "Décision requise"},
                    ]
                },
            }

        with patch.dict(os.environ, {"TEST_GOOGLE_ACCESS": "google-token"}), patch(
            "founder_os.connectors.gmail.request_json",
            side_effect=response,
        ) as request:
            connector = GmailConnector(
                {
                    "access_token_env": "TEST_GOOGLE_ACCESS",
                    "detail_workers": 1,
                    "max_results": 5,
                }
            )
            events = connector.poll(NOW)
        self.assertEqual(events[0].title, "Élodie: Décision requise")
        self.assertEqual(request.call_count, 2)
        self.assertTrue(
            all(call.kwargs["headers"]["Authorization"] == "Bearer google-token" for call in request.call_args_list)
        )

    def test_calendar_poll_uses_a_bounded_time_window(self) -> None:
        payload = {
            "items": [
                {
                    "id": "event-1",
                    "summary": "Comité stratégie",
                    "status": "confirmed",
                    "updated": NOW.isoformat(),
                    "start": {"dateTime": "2026-08-01T10:10:00Z"},
                    "end": {"dateTime": "2026-08-01T10:40:00Z"},
                    "attendees": [],
                }
            ]
        }
        with patch.dict(os.environ, {"TEST_GOOGLE_ACCESS": "google-token"}), patch(
            "founder_os.connectors.calendar.request_json",
            return_value=payload,
        ) as request:
            connector = GoogleCalendarConnector(
                {"access_token_env": "TEST_GOOGLE_ACCESS", "horizon_hours": 8}
            )
            events = connector.poll(NOW)
        self.assertEqual(events[0].title, "PREP Comité stratégie")
        query = request.call_args.kwargs["query"]
        self.assertEqual(query["timeMin"], "2026-08-01T08:00:00+00:00")
        self.assertEqual(query["timeMax"], "2026-08-01T18:00:00+00:00")

    def test_calendar_follows_bounded_page_tokens(self) -> None:
        def item(event_id: str, minute: int) -> dict:
            return {
                "id": event_id,
                "summary": f"Réunion {event_id}",
                "status": "confirmed",
                "updated": NOW.isoformat(),
                "start": {"dateTime": f"2026-08-01T10:{minute:02d}:00Z"},
                "end": {"dateTime": f"2026-08-01T10:{minute + 5:02d}:00Z"},
                "attendees": [],
            }

        pages = (
            {"items": [item("one", 10)], "nextPageToken": "next"},
            {"items": [item("two", 20)]},
        )
        with patch.dict(os.environ, {"TEST_GOOGLE_ACCESS": "google-token"}), patch(
            "founder_os.connectors.calendar.request_json",
            side_effect=pages,
        ) as request:
            connector = GoogleCalendarConnector({
                "access_token_env": "TEST_GOOGLE_ACCESS",
                "page_size": 1,
                "max_events": 2,
            })
            events = connector.poll(NOW)
        self.assertEqual(len(events), 2)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1].kwargs["query"]["pageToken"], "next")


if __name__ == "__main__":
    unittest.main()
