"""Connectors for user-controlled JSON feeds and append-only JSONL inboxes."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from founder_os.connectors.base import (
    Connector,
    ConnectorConfigurationError,
    ConnectorError,
    ConnectorUnavailableError,
    configured_secret,
)
from founder_os.connectors.http_client import request_json
from founder_os.models import Event


class JsonFeedConnector(Connector):
    name = "feed"

    def __init__(self, config: Mapping[str, Any], *, source: str) -> None:
        super().__init__(config)
        self.source = source
        self.feed_url = str(config.get("feed_url", "")).strip()
        if not self.feed_url:
            raise ConnectorConfigurationError(f"{source}.feed_url is required")
        token_env = str(config.get("token_env", "")).strip()
        self.token = configured_secret(config, "token_env") if token_env else ""
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 6)))
        self.max_response_bytes = max(1024, int(config.get("max_response_bytes", 2 * 1024 * 1024)))

    def poll(self, now: datetime) -> list[Event]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = request_json(
            self.feed_url,
            headers=headers,
            timeout=self.request_timeout,
            max_response_bytes=self.max_response_bytes,
        )
        rows = payload.get("events") or []
        if not isinstance(rows, list):
            raise ConnectorError(f"{self.source} feed response must contain an events array")
        return [Event.from_mapping(row, source=self.source) for row in rows]


class JsonlInboxConnector(Connector):
    """Tail normalized events exported by a local tool without scraping private transcripts."""

    name = "jsonl_inbox"
    emits_snapshot = False

    def __init__(self, config: Mapping[str, Any], *, source: str) -> None:
        super().__init__(config)
        self.source = source
        raw_path = str(config.get("inbox_path", "")).strip()
        if not raw_path:
            raise ConnectorConfigurationError(f"{source}.inbox_path is required")
        self.path = Path(raw_path).expanduser()
        self._offset = 0
        self._identity: tuple[int, int] | None = None

    def poll(self, now: datetime) -> list[Event]:
        if not self.path.exists():
            raise ConnectorUnavailableError(f"{self.source} inbox is missing: {self.path}")
        stat = self.path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if self._identity != identity or stat.st_size < self._offset:
            self._offset = 0
            self._identity = identity
        events: list[Event] = []
        try:
            with self.path.open("rb") as stream:
                stream.seek(self._offset)
                while True:
                    start = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        stream.seek(start)
                        break
                    next_offset = stream.tell()
                    if not line.strip():
                        self._offset = next_offset
                        continue
                    payload = json.loads(line.decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("JSONL item must be an object")
                    events.append(Event.from_mapping(payload, source=self.source))
                    self._offset = next_offset
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ConnectorError(f"cannot read {self.source} inbox {self.path}: {exc}") from exc
        return events


class LinkedInConnector(JsonFeedConnector):
    """LinkedIn adapter backed by a user-controlled webhook/feed bridge."""

    name = "linkedin"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config, source="linkedin")


class ClaudeConnector(JsonlInboxConnector):
    name = "claude"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config, source="claude")


class ChatGPTCodexConnector(JsonlInboxConnector):
    name = "chatgpt_codex"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config, source="chatgpt_codex")
