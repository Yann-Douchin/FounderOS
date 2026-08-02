"""Opt-in Home Assistant availability context for capacity governance."""

from __future__ import annotations

import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Mapping

from founder_os.connectors.base import Connector, ConnectorConfigurationError, ConnectorError, configured_secret
from founder_os.connectors.http_client import request_json
from founder_os.models import Event, parse_datetime


class HomeAssistantConnector(Connector):
    name = "home_assistant"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.token = configured_secret(config, "token_env", self.secrets)
        self.endpoint = str(config.get("endpoint") or "").rstrip("/")
        if not self.endpoint:
            raise ConnectorConfigurationError("home_assistant.endpoint is required")
        self.entities = [dict(item) for item in config.get("entities", []) if isinstance(item, Mapping)]
        if not self.entities:
            raise ConnectorConfigurationError("home_assistant.entities must contain configured availability entities")
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 4)))

    def poll(self, now: datetime) -> list[Event]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        events: list[Event] = []
        for config in self.entities:
            entity_id = str(config.get("entity_id") or "").strip()
            if not entity_id:
                raise ConnectorConfigurationError("each Home Assistant entity requires entity_id")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Home Assistant poll exceeded {self.poll_timeout_seconds:.0f} seconds")
            payload = request_json(
                f"{self.endpoint}/api/states/{urllib.parse.quote(entity_id, safe='._')}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=min(self.request_timeout, remaining),
                retries=0,
                deadline_monotonic=deadline,
            )
            state = str(payload.get("state") or "unknown").casefold()
            unavailable_states = {str(value).casefold() for value in config.get("unavailable_states", ["not_home", "off", "unavailable"])}
            unavailable = state in unavailable_states
            attributes = payload.get("attributes") or {}
            if not isinstance(attributes, Mapping):
                raise ConnectorError("Home Assistant state attributes must be an object")
            owner = str(config.get("owner") or attributes.get("friendly_name") or "self")
            updated = parse_datetime(payload.get("last_updated"), default=now) or now
            events.append(Event(
                id=f"home_assistant:{entity_id}",
                source="home_assistant",
                title=f"{owner} {'unavailable' if unavailable else 'available'}",
                kind="availability",
                priority=50 if unavailable else 20,
                action_required=False,
                urgency="normal",
                impact="medium",
                occurred_at=updated,
                expires_at=now + timedelta(minutes=max(2.0, self.poll_interval_seconds * 2 / 60)),
                dedupe_key=f"home_assistant:{entity_id}",
                metadata={
                    "person": owner,
                    "owner": owner,
                    "availability": "unavailable" if unavailable else "available",
                    "state": state,
                    "start_at": updated.isoformat(),
                    "end_at": (now + timedelta(hours=float(config.get("assumed_hours", 8)))).isoformat(),
                },
            ))
        return events
