"""Slack connector using the read-only conversations.history Web API method."""

from __future__ import annotations

import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from founder_os.connectors.base import Connector, ConnectorConfigurationError, ConnectorError, configured_secret
from founder_os.connectors.http_client import request_json
from founder_os.models import Event


DEFAULT_RISK_KEYWORDS = (
    "waiting for", "en attente", "need access", "besoin d’accès",
    "cannot access", "pas accès", "credentials", "identifiants",
    "permission missing", "token missing", "not working", "ne fonctionne pas", "inert",
)
DEFAULT_DECISION_KEYWORDS = (
    "please confirm", "merci de confirmer", "decision needed", "décision requise",
    "who owns", "qui est responsable", "please clarify", "merci de clarifier",
)


class SlackConnector(Connector):
    name = "slack"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.token = configured_secret(config, "token_env", self.secrets)
        self.channel_ids = [str(value) for value in config.get("channel_ids", [])]
        if not self.channel_ids:
            raise ConnectorConfigurationError("slack.channel_ids must contain at least one conversation id")
        self.channel_names = {str(key): str(value) for key, value in config.get("channel_names", {}).items()}
        self.mention_markers = [str(value).casefold() for value in config.get("mention_markers", [])]
        self.urgent_keywords = [
            str(value).casefold()
            for value in config.get("urgent_keywords", ["blocked", "bloqué", "urgent", "incident", "down", "approval"])
        ]
        self.risk_keywords = [str(value).casefold() for value in config.get("risk_keywords", DEFAULT_RISK_KEYWORDS)]
        self.decision_keywords = [
            str(value).casefold() for value in config.get("decision_keywords", DEFAULT_DECISION_KEYWORDS)
        ]
        self.lookback_minutes = float(config.get("lookback_minutes", 30))
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 6)))
        self.endpoint = str(config.get("endpoint", "https://slack.com/api")).rstrip("/")
        self.workspace_url = str(config.get("workspace_url", "")).rstrip("/")
        self.page_size = min(100, max(1, int(config.get("page_size", 50))))
        self.max_pages = min(10, max(1, int(config.get("max_pages", 3))))

    def poll(self, now: datetime) -> list[Event]:
        oldest = (now - timedelta(minutes=self.lookback_minutes)).timestamp()
        deadline = time.monotonic() + self.poll_timeout_seconds
        events: list[Event] = []
        for channel_id in self.channel_ids:
            cursor = ""
            for _ in range(self.max_pages):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Slack poll exceeded {self.poll_timeout_seconds:.0f} seconds")
                payload = request_json(
                    f"{self.endpoint}/conversations.history",
                    query={
                        "channel": channel_id,
                        "oldest": f"{oldest:.6f}",
                        "limit": self.page_size,
                        "cursor": cursor or None,
                    },
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=min(self.request_timeout, remaining),
                    retries=0,
                )
                if not payload.get("ok"):
                    error_code = str(payload.get("error") or "")
                    if not re.fullmatch(r"[a-z0-9_-]{1,64}", error_code):
                        error_code = "unknown_error"
                    raise ConnectorError(
                        f"Slack conversations.history failed: {error_code}"
                    )
                messages = payload.get("messages") or []
                if not isinstance(messages, list) or not all(isinstance(message, Mapping) for message in messages):
                    raise ConnectorError("Slack response did not contain a message list")
                for message in messages:
                    event = self._normalize(channel_id, message, now)
                    if event:
                        events.append(event)
                cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "")
                if not payload.get("has_more") and not cursor:
                    break
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
        risk_keywords = getattr(self, "risk_keywords", DEFAULT_RISK_KEYWORDS)
        decision_keywords = getattr(self, "decision_keywords", DEFAULT_DECISION_KEYWORDS)
        risk = any(keyword in folded for keyword in risk_keywords)
        decision = any(keyword in folded for keyword in decision_keywords)
        if self.mention_markers and not (mentioned or urgent or risk or decision):
            return None
        ts = str(message.get("ts") or "0")
        if ts == "0":
            raise ConnectorError("Slack message is missing its timestamp")
        try:
            occurred_at = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (OSError, OverflowError, TypeError, ValueError):
            occurred_at = now
        channel = self.channel_names.get(channel_id, channel_id)
        title = f"#{channel} {text}"
        message_url = ""
        workspace_url = getattr(self, "workspace_url", "")
        if workspace_url:
            message_url = (
                f"{workspace_url}/archives/{urllib.parse.quote(channel_id, safe='')}"
                f"/p{ts.replace('.', '')}"
            )
        return Event(
            id=f"slack:{channel_id}:{ts}",
            source="slack",
            kind="blocker" if urgent else "waiting" if risk else "message",
            title=title,
            priority=86 if urgent else 78 if risk else 74 if decision else 68 if mentioned else 52,
            action_required=mentioned or urgent or risk or decision,
            urgency="high" if urgent or risk else "normal",
            impact="high" if urgent or risk else "medium",
            occurred_at=occurred_at,
            expires_at=occurred_at + timedelta(hours=2),
            dedupe_key=f"slack:{channel_id}:{message.get('thread_ts') or ts}",
            url=message_url,
            metadata={
                "channel_id": channel_id,
                "user": message.get("user"),
                "thread_ts": message.get("thread_ts"),
                "signal": "urgent" if urgent else "dependency" if risk else "decision" if decision else "mention",
            },
        )
