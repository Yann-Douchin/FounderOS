from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from founder_os.connectors.base import (
    ConnectorConfigurationError,
    ConnectorError,
    ConnectorUnavailableError,
)
from founder_os.connectors.feed import JsonlInboxConnector


NOW = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)


class FeedSafetyTests(unittest.TestCase):
    def test_inbox_path_is_required(self) -> None:
        with self.assertRaises(ConnectorConfigurationError):
            JsonlInboxConnector({}, source="claude")

    def test_missing_inbox_is_visible_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            connector = JsonlInboxConnector(
                {"inbox_path": str(Path(folder) / "missing.jsonl")},
                source="claude",
            )
            with self.assertRaises(ConnectorUnavailableError):
                connector.poll(NOW)

    def test_malformed_complete_line_is_not_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            path.write_text("not-json\n", encoding="utf-8")
            connector = JsonlInboxConnector({"inbox_path": str(path)}, source="claude")
            with self.assertRaises(ConnectorError):
                connector.poll(NOW)
            with self.assertRaises(ConnectorError):
                connector.poll(NOW)


if __name__ == "__main__":
    unittest.main()
