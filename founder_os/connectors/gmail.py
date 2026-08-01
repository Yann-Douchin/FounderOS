"""Gmail connector for recent unread messages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any, Mapping

from founder_os.connectors.base import Connector, configured_secret
from founder_os.connectors.http import request_json
from founder_os.models import Event


class GmailConnector(Connector):
    name = "gmail"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.access_token = configured_secret(config, "access_token_env")
        self.query = str(config.get("query", "is:unread newer_than:2d"))
        self.vip_senders = {str(value).casefold() for value in config.get("vip_senders", [])}
        self.max_results = min(25, max(1, int(config.get("max_results", 10))))

    def poll(self, now: datetime) -> list[Event]:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        listing = request_json(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            query={"q": self.query, "maxResults": self.max_results},
            headers=headers,
        )
        events = []
        for item in listing.get("messages") or []:
            message_id = str(item.get("id") or "")
            if not message_id:
                continue
            message = request_json(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
                query={"format": "metadata"},
                headers=headers,
            )
            events.append(self._normalize(message, now))
        return events

    def _normalize(self, message: Mapping[str, Any], now: datetime) -> Event:
        headers = {
            str(item.get("name", "")).casefold(): str(item.get("value", ""))
            for item in ((message.get("payload") or {}).get("headers") or [])
        }
        subject = " ".join((headers.get("subject") or "Sans objet").split())
        sender_name, sender_email = parseaddr(headers.get("from", ""))
        sender_label = sender_name or sender_email or "Expéditeur inconnu"
        sender_folded = sender_email.casefold()
        vip = any(value in sender_folded for value in self.vip_senders)
        labels = set(message.get("labelIds") or [])
        important = "IMPORTANT" in labels
        internal_ms = message.get("internalDate")
        try:
            occurred_at = datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            occurred_at = now
        title = f"{sender_label}: {subject}"
        return Event(
            id=f"gmail:{message.get('id')}",
            source="gmail",
            kind="email",
            title=title,
            body=str(message.get("snippet") or ""),
            priority=82 if vip else 70 if important else 55,
            action_required=True,
            urgency="high" if vip else "normal",
            impact="high" if vip else "medium",
            occurred_at=occurred_at,
            expires_at=occurred_at + timedelta(days=2),
            dedupe_key=f"gmail-thread:{message.get('threadId') or message.get('id')}",
            url=f"https://mail.google.com/mail/u/0/#inbox/{message.get('id')}",
            metadata={"from": headers.get("from", ""), "subject": subject, "labels": sorted(labels)},
        )
