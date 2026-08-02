"""Superhuman reminder state through the user's authorized Gmail account."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Mapping

from founder_os.connectors.gmail import GmailConnector
from founder_os.models import Event


class SuperhumanReminderConnector(GmailConnector):
    name = "superhuman"

    def __init__(self, config: Mapping[str, Any]) -> None:
        values = dict(config)
        values.setdefault("query", "label:reminder newer_than:30d")
        values.setdefault("queries", [{"query": values["query"], "direction": "incoming"}])
        values.setdefault("max_results", 25)
        super().__init__(values)

    def _normalize(self, message: Mapping[str, Any], now: datetime) -> Event:
        event = super()._normalize(message, now)
        message_id = event.id.split(":", 1)[-1]
        metadata = dict(event.metadata)
        metadata.update({
            "reminder": True,
            "obligation": True,
            "obligation_type": "commitment",
        })
        return replace(
            event,
            id=f"superhuman:{message_id}",
            source="superhuman",
            kind="waiting",
            priority=max(72, event.priority),
            action_required=True,
            dedupe_key=f"superhuman:{metadata.get('thread_id') or message_id}",
            metadata=metadata,
        )
