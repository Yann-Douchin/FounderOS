"""Slack connector using the read-only conversations.history Web API method."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from founder_os.connectors.base import Connector, ConnectorConfigurationError, ConnectorError, configured_secret
from founder_os.connectors.http import request_json
from founder_os.models import Event


class SlackConnector(Connector):
    name = "slack"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.token = configured_secret(config, "token_env")
        self.channel_ids = [str(value) for value in config.get("channel_ids", [])]
        if not self.channel_ids:
            raise ConnectorConfigurationError("slack.channel_ids must contain at least one conversation id")
        self.channel_names = {str(key): str(value) for key, value in config.get("channel_names", {}).items()}
        self.mention_markers = [str(value).casefold() for value in config.get("mention_markers", [])]
        self.urgent_keywords = [
            str(value).casefold()
            for value in config.get("urgent_keywords", ["blocked", "urgent", "incident", "down", "approval"])
        ]
        self.lookback_minutes = float(config.get("lookback_minutes", 30))

    def poll(self, now: datetime) -> list[Event]:
        oldest = (now - timedelta(minutes=self.lookback_minutes)).timestamp()
        events: list[Event] = []
        for channel_id in self.channel_ids:
            payload = request_json(
                "https://slack.com/api/conversations.history",
                query={"channel": channel_id, "oldest": f"{oldest:.6f}", "limit": 15},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            if not payload.get("ok"):
                raise ConnectorError(f"Slack conversations.history failed: {payload.get('error', 'unknown_error')}")
            for message in payload.get("messages") or []:
                event = self._normalize(channel_id, message, now)
                if event:
                    events.append(event)
        return events

    def _normalize(self, channel_id: str, message: Mapping[str, Any], now: datetime) -> Event | None:
        if message.get("subtype") in {"message_deleted", "channel_join", "channel_leave"}:
            return None
        text = " ".join(str(message.get("text") or "").split())
        if not text:
            return None
        folded = text.casefold()
        mentioned = any(marker and marker in folded for marker in self.mention_markers)
        urgent = any(keyword in folded for keyword in self.urgent_keywords)
        if self.mention_markers and not (mentioned or urgent):
            return None
        ts = str(message.get("ts") or "0")
        try:
            occurred_at = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except ValueError:
            occurred_at = now
        channel = self.channel_names.get(channel_id, channel_id)
        title = f"#{channel} {text}"
        return Event(
            id=f"slack:{channel_id}:{ts}",
            source="slack",
            kind="blocker" if urgent else "message",
            title=title,
            priority=86 if urgent else 68 if mentioned else 52,
            action_required=mentioned or urgent,
            urgency="high" if urgent else "normal",
            impact="high" if urgent else "medium",
            occurred_at=occurred_at,
            expires_at=occurred_at + timedelta(hours=2),
            dedupe_key=f"slack:{channel_id}:{message.get('thread_ts') or ts}",
            metadata={"channel_id": channel_id, "user": message.get("user"), "thread_ts": message.get("thread_ts")},
        )
