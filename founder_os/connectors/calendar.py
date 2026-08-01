"""Google Calendar connector for near-term commitments."""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Mapping

from founder_os.connectors.base import Connector, configured_secret
from founder_os.connectors.http import request_json
from founder_os.models import Event, parse_datetime


class GoogleCalendarConnector(Connector):
    name = "calendar"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.access_token = configured_secret(config, "access_token_env")
        self.calendar_id = str(config.get("calendar_id", "primary"))
        self.horizon_hours = max(1.0, float(config.get("horizon_hours", 8)))

    def poll(self, now: datetime) -> list[Event]:
        calendar_id = urllib.parse.quote(self.calendar_id, safe="")
        payload = request_json(
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
            query={
                "timeMin": now.isoformat(),
                "timeMax": (now + timedelta(hours=self.horizon_hours)).isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 20,
            },
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        return [event for item in payload.get("items") or [] if (event := self._normalize(item, now))]

    @staticmethod
    def _normalize(item: Mapping[str, Any], now: datetime) -> Event | None:
        if item.get("status") == "cancelled":
            return None
        start_value = (item.get("start") or {}).get("dateTime")
        if not start_value:
            return None
        start_at = parse_datetime(start_value)
        if not start_at:
            return None
        end_at = parse_datetime((item.get("end") or {}).get("dateTime")) or start_at + timedelta(hours=1)
        minutes = (start_at - now).total_seconds() / 60
        self_attendee = next((person for person in item.get("attendees") or [] if person.get("self")), {})
        needs_action = self_attendee.get("responseStatus") == "needsAction"
        summary = " ".join(str(item.get("summary") or "Rendez-vous").split())
        if minutes <= 5:
            priority, urgency = 94, "critical"
        elif minutes <= 15:
            priority, urgency = 84, "high"
        elif minutes <= 60:
            priority, urgency = 68, "normal"
        else:
            priority, urgency = 50, "normal"
        prefix = "RSVP " if needs_action else ""
        return Event(
            id=f"calendar:{item.get('id')}:{start_at.isoformat()}",
            source="calendar",
            kind="meeting",
            title=f"{prefix}{summary}",
            body=str(item.get("location") or ""),
            priority=priority + (6 if needs_action else 0),
            action_required=needs_action or minutes <= 15,
            urgency=urgency,
            impact="high" if minutes <= 15 else "medium",
            occurred_at=parse_datetime(item.get("updated"), default=now) or now,
            due_at=start_at,
            expires_at=end_at,
            dedupe_key=f"calendar:{item.get('id')}:{start_at.isoformat()}",
            url=str(item.get("htmlLink") or ""),
            metadata={"start_at": start_at.isoformat(), "end_at": end_at.isoformat(), "location": item.get("location")},
        )
