from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from founder_os.connectors.commerce import ShopifyConnector, StripeConnector
from founder_os.connectors.drive import GoogleDriveConnector, GoogleSheetsConnector
from founder_os.connectors.github import DeploymentConnector, GitHubConnector
from founder_os.connectors.home_assistant import HomeAssistantConnector
from founder_os.connectors.notion import NotionConnector
from founder_os.connectors.observability import PostHogConnector, SentryConnector
from founder_os.connectors.superhuman import SuperhumanReminderConnector


NOW = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)


class ClosureConnectorTests(unittest.TestCase):
    def test_notion_emits_a_structured_decision_and_document_evidence(self) -> None:
        response = {
            "results": [{
                "id": "page-1",
                "url": "https://notion.so/page-1",
                "last_edited_time": NOW.isoformat(),
                "parent": {"database_id": "decisions"},
                "properties": {
                    "Name": {"type": "title", "title": [{"plain_text": "Approve launch plan"}]},
                    "Status": {"status": {"name": "Open"}},
                    "Owner": {"people": [{"name": "Yann"}]},
                    "Project": {"rich_text": [{"plain_text": "Launch"}]},
                },
            }],
            "has_more": False,
        }
        with patch.dict(os.environ, {"TEST_NOTION": "token"}), patch(
            "founder_os.connectors.notion.request_json", return_value=response
        ) as request:
            connector = NotionConnector({"token_env": "TEST_NOTION", "database_ids": ["decisions"]})
            events = connector.poll(NOW)
        self.assertEqual(events[0].source, "notion")
        self.assertTrue(events[0].action_required)
        self.assertEqual(events[0].metadata["project"], "Launch")
        self.assertIn("Notion-Version", request.call_args.kwargs["headers"])

    def test_notion_not_approved_is_never_validation_evidence(self) -> None:
        connector = NotionConnector.__new__(NotionConnector)
        connector.action_keywords = ("approve", "approved")
        event = connector._normalize({
            "id": "page-negative",
            "url": "https://notion.so/page-negative",
            "last_edited_time": NOW.isoformat(),
            "parent": {"database_id": "decisions"},
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Approve launch"}]},
                "Status": {"status": {"name": "Not approved"}},
            },
        }, NOW)
        self.assertTrue(event.action_required)
        self.assertNotEqual(event.metadata["gate_status"].get("validation"), "satisfied")
        self.assertNotIn("validation", event.metadata["evidence_categories"])

    def test_drive_emits_document_evidence_with_custom_project_metadata(self) -> None:
        response = {"files": [{
            "id": "file-1",
            "name": "Launch validation matrix",
            "modifiedTime": NOW.isoformat(),
            "webViewLink": "https://drive.google.com/file-1",
            "owners": [{"displayName": "Yann"}],
            "properties": {
                "founderos_project": "Launch",
                "founderos_status": "approved",
                "founderos_evidence": "market,language",
            },
        }]}
        with patch.dict(os.environ, {"TEST_GOOGLE": "token"}), patch(
            "founder_os.connectors.drive.request_json", return_value=response
        ):
            connector = GoogleDriveConnector({
                "access_token_env": "TEST_GOOGLE",
                "folder_ids": ["folder-1"],
            })
            event = connector.poll(NOW)[0]
        self.assertEqual(event.metadata["evidence_categories"], ["market", "language"])
        self.assertEqual(event.metadata["gate_status"]["validation"], "satisfied")

    def test_sheets_turns_feedback_and_proof_columns_into_a_governed_event(self) -> None:
        response = {"valueRanges": [{
            "range": "Proof!A1:I10",
            "values": [
                ["ID", "Title", "Status", "Owner", "Project", "Feedback", "Decision", "market", "language"],
                ["row-1", "Validate export", "Open", "Yann", "Launch", "Customer feedback", "QTY-42", "yes", "yes"],
            ],
        }]}
        with patch.dict(os.environ, {"TEST_GOOGLE": "token"}), patch(
            "founder_os.connectors.drive.request_json", return_value=response
        ):
            connector = GoogleSheetsConnector({
                "access_token_env": "TEST_GOOGLE",
                "spreadsheets": [{
                    "id": "sheet-1",
                    "ranges": ["Proof!A1:I10"],
                    "evidence_columns": ["market", "language"],
                }],
            })
            event = connector.poll(NOW)[0]
        self.assertTrue(event.metadata["feedback"])
        self.assertEqual(event.metadata["decision"], "QTY-42")
        self.assertEqual(event.metadata["evidence_categories"], ["market", "language"])

    def test_github_exposes_failed_deployment_and_requested_review(self) -> None:
        responses = [
            {"workflow_runs": [{
                "id": 1,
                "name": "Deploy",
                "display_title": "Launch",
                "status": "completed",
                "conclusion": "failure",
                "updated_at": NOW.isoformat(),
                "html_url": "https://github.test/run/1",
                "head_branch": "main",
            }]},
            {"items": [{
                "number": 2,
                "title": "Release",
                "updated_at": NOW.isoformat(),
                "html_url": "https://github.test/pr/2",
                "user": {"login": "alex"},
                "labels": [],
            }]},
        ]
        with patch.dict(os.environ, {"TEST_GITHUB": "token"}), patch(
            "founder_os.connectors.github.request_json", side_effect=responses
        ):
            connector = GitHubConnector({
                "token_env": "TEST_GITHUB",
                "repositories": ["acme/product"],
                "review_login": "yann",
            })
            events = connector.poll(NOW)
        self.assertEqual(events[0].metadata["deployment_status"], "failed")
        self.assertTrue(events[1].action_required)

    def test_generic_deployment_connector_marks_success_as_proof(self) -> None:
        with patch("founder_os.connectors.github.request_json", return_value={"deployments": [{
            "id": "dep-1", "project": "Launch", "status": "success", "updated_at": NOW.isoformat()
        }]}):
            connector = DeploymentConnector({"endpoint": "https://deploy.example/api/status"})
            event = connector.poll(NOW)[0]
        self.assertEqual(event.metadata["evidence_categories"], ["deployment"])
        self.assertFalse(event.action_required)

    def test_generic_ci_name_never_counts_as_a_github_deployment(self) -> None:
        responses = [
            {"workflow_runs": [{
                "id": 3,
                "name": "Preproduction CI",
                "status": "completed",
                "conclusion": "success",
                "updated_at": NOW.isoformat(),
            }]},
            {"items": []},
        ]
        with patch.dict(os.environ, {"TEST_GITHUB": "token"}), patch(
            "founder_os.connectors.github.request_json", side_effect=responses
        ):
            event = GitHubConnector({
                "token_env": "TEST_GITHUB",
                "repositories": ["acme/product"],
                "deployment_workflows": ["production"],
            }).poll(NOW)[0]
        self.assertEqual(event.metadata["deployment_status"], "pending")
        self.assertEqual(event.metadata["evidence_categories"], ["code"])
        self.assertEqual(event.kind, "information")

    def test_sentry_and_posthog_produce_failure_and_success_evidence(self) -> None:
        with patch.dict(os.environ, {"TEST_SENTRY": "token"}), patch(
            "founder_os.connectors.observability.request_json",
            return_value=[{
                "id": "issue-1", "title": "Checkout error", "level": "error",
                "count": "20", "userCount": 4, "lastSeen": NOW.isoformat(),
            }],
        ) as request:
            event = SentryConnector({
                "token_env": "TEST_SENTRY", "organization": "acme", "projects": ["checkout"]
            }).poll(NOW)[0]
        self.assertTrue(event.action_required)
        self.assertEqual(request.call_args.kwargs["root"], "array")
        with patch.dict(os.environ, {"TEST_POSTHOG": "token"}), patch(
            "founder_os.connectors.observability.request_json", return_value={"results": [[0.01]]}
        ):
            event = PostHogConnector({
                "token_env": "TEST_POSTHOG",
                "project_id": "1",
                "checks": [{
                    "name": "Error rate", "project": "Launch", "query": {"kind": "HogQLQuery"},
                    "value_path": ["results", 0, 0], "comparator": "gte", "threshold": 0.05,
                }],
            }).poll(NOW)[0]
        self.assertFalse(event.action_required)
        self.assertEqual(event.metadata["evidence_categories"], ["analytics"])

    def test_shopify_readiness_detects_missing_scope(self) -> None:
        responses = [
            {"access_scopes": [{"handle": "read_orders"}]},
            {"data": {"shop": {"name": "Merchant", "primaryDomain": {"url": "https://merchant.test"}}, "productsCount": {"count": 4}}},
        ]
        with patch.dict(os.environ, {"TEST_SHOPIFY": "token"}), patch(
            "founder_os.connectors.commerce.request_json", side_effect=responses
        ):
            event = ShopifyConnector({
                "token_env": "TEST_SHOPIFY", "shop": "merchant.myshopify.com",
                "required_scopes": ["read_products"],
            }).poll(NOW)[0]
        self.assertTrue(event.action_required)
        self.assertEqual(event.metadata["gate_status"]["access"], "blocked")

    def test_stripe_surfaces_overdue_invoice_and_dispute(self) -> None:
        responses = [
            {"data": [{
                "id": "in_1", "customer_email": "client@example.test", "amount_due": 12000,
                "currency": "eur", "created": int(NOW.timestamp()), "due_date": int(NOW.timestamp()) - 60,
            }]},
            {"data": [{
                "id": "dp_1", "status": "needs_response", "reason": "fraudulent",
                "created": int(NOW.timestamp()), "evidence_details": {"due_by": int(NOW.timestamp()) + 3600},
            }]},
        ]
        with patch.dict(os.environ, {"TEST_STRIPE": "token"}), patch(
            "founder_os.connectors.commerce.request_json", side_effect=responses
        ):
            events = StripeConnector({"token_env": "TEST_STRIPE"}).poll(NOW)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event.action_required for event in events))

    def test_closed_stripe_dispute_is_evidence_not_an_action(self) -> None:
        event = StripeConnector._dispute({
            "id": "dp_won",
            "status": "won",
            "created": int(NOW.timestamp()),
            "evidence_details": {},
        }, NOW)
        self.assertFalse(event.action_required)
        self.assertEqual(event.metadata["evidence_status"], "present")
        self.assertIn("validation", event.metadata["evidence_categories"])

    def test_superhuman_reminder_and_home_assistant_availability(self) -> None:
        gmail_responses = [
            {"messages": [{"id": "m1"}]},
            {
                "id": "m1", "threadId": "t1", "internalDate": str(int(NOW.timestamp() * 1000)),
                "labelIds": ["IMPORTANT"],
                "payload": {"headers": [
                    {"name": "From", "value": "Client <client@example.test>"},
                    {"name": "Subject", "value": "Follow up"},
                ]},
            },
        ]
        with patch.dict(os.environ, {"TEST_GOOGLE": "token"}), patch(
            "founder_os.connectors.gmail.request_json", side_effect=gmail_responses
        ):
            event = SuperhumanReminderConnector({"access_token_env": "TEST_GOOGLE"}).poll(NOW)[0]
        self.assertEqual(event.source, "superhuman")
        self.assertTrue(event.metadata["reminder"])
        with patch.dict(os.environ, {"TEST_HA": "token"}), patch(
            "founder_os.connectors.home_assistant.request_json",
            return_value={"state": "not_home", "last_updated": NOW.isoformat(), "attributes": {}},
        ):
            event = HomeAssistantConnector({
                "token_env": "TEST_HA",
                "endpoint": "http://127.0.0.1:8123",
                "entities": [{"entity_id": "person.alex", "owner": "Alex"}],
            }).poll(NOW)[0]
        self.assertEqual(event.metadata["availability"], "unavailable")


if __name__ == "__main__":
    unittest.main()
