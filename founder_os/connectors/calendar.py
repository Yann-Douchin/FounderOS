"""Google Calendar connector for near-term commitments."""

from __future__ import annotations

import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Mapping

from founder_os.connectors.base import Connector, ConnectorConfigurationError, ConnectorError
from founder_os.connectors.google_oauth import GoogleAccessTokenProvider
from founder_os.connectors.http_client import ConnectorHTTPError, request_json
from founder_os.models import Event, parse_datetime, parse_local_date


DEFAULT_READINESS_KEYWORDS = (
    "launch", "go-live", "client", "customer", "investor", "investisseur",
    "demo", "contrat", "contract", "stratégie", "strategy",
)


class GoogleCalendarConnector(Connector):
    name = "calendar"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.token_provider = GoogleAccessTokenProvider(config, secrets=self.secrets)
        self.calendar_id = str(config.get("calendar_id", "primary"))
        self.horizon_hours = max(1.0, float(config.get("horizon_hours", 8)))
        self.readiness_minutes = max(5.0, float(config.get("readiness_minutes", 30)))
        values = config.get("readiness_keywords", DEFAULT_READINESS_KEYWORDS)
        if not isinstance(values, (list, tuple)):
            raise ConnectorConfigurationError("calendar.readiness_keywords must be a list")
        self.readiness_keywords = tuple(str(value).strip().casefold() for value in values if str(value).strip())
        self.timezone = str(config.get("timezone", "Europe/Madrid"))
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 6)))
        self.http_retries = min(2, max(0, int(config.get("http_retries", 1))))
        self.page_size = min(250, max(1, int(config.get("page_size", 20))))
        self.max_events = min(500, max(self.page_size, int(config.get("max_events", 100))))
        self.max_pages = min(50, max(1, int(config.get("max_pages", 20))))
        self.endpoint = str(config.get("endpoint", "https://www.googleapis.com/calendar/v3"))

    def poll(self, now: datetime) -> list[Event]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        calendar_id = urllib.parse.quote(self.calendar_id, safe="")
        url = f"{self.endpoint.rstrip('/')}/calendars/{calendar_id}/events"
        base_query = {
            "timeMin": now.isoformat(),
            "timeMax": (now + timedelta(hours=self.horizon_hours)).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        items: list[Mapping[str, Any]] = []
        page_token = ""
        page_count = 0
        while len(items) < self.max_events:
            if page_count >= self.max_pages:
                raise ConnectorError(f"Calendar pagination exceeded {self.max_pages} pages")
            query = {
                **base_query,
                "maxResults": min(self.page_size, self.max_events - len(items)),
                "pageToken": page_token or None,
            }
            payload = self._request_events(url, query, now, deadline)
            page_count += 1
            page_items = payload.get("items") or []
            if not isinstance(page_items, list) or not all(isinstance(item, Mapping) for item in page_items):
                raise ConnectorError("Calendar response did not contain an event list")
            items.extend(page_items)
            next_token = str(payload.get("nextPageToken") or "")
            if not next_token:
                break
            if next_token == page_token:
                raise ConnectorError("Calendar pagination returned an invalid page token")
            page_token = next_token
        return [event for item in items if (event := self._normalize(item, now))]

    def _request_events(
        self,
        url: str,
        query: Mapping[str, Any],
        now: datetime,
        deadline: float,
    ) -> dict[str, Any]:
        token = self.token_provider.token(now, deadline_monotonic=deadline)
        timeout = self._remaining_timeout(deadline)
        try:
            return request_json(
                url,
                query=query,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
                retries=self.http_retries,
                deadline_monotonic=deadline,
            )
        except ConnectorHTTPError as exc:
            if exc.status_code != 401 or not self.token_provider.refreshable:
                raise
        self.token_provider.invalidate()
        token = self.token_provider.token(now, deadline_monotonic=deadline)
        timeout = self._remaining_timeout(deadline)
        return request_json(
            url,
            query=query,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            retries=0,
            deadline_monotonic=deadline,
        )

    def _remaining_timeout(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Calendar poll exceeded {self.poll_timeout_seconds:.0f} seconds")
        return min(self.request_timeout, remaining / (self.http_retries + 1))

    def _normalize(self, item: Mapping[str, Any], now: datetime) -> Event | None:
        if item.get("status") == "cancelled":
            return None
        event_id = str(item.get("id") or "").strip()
        if not event_id:
            raise ConnectorError("Calendar event is missing its id")
        start = item.get("start") or {}
        end = item.get("end") or {}
        all_day = bool(start.get("date") and not start.get("dateTime"))
        start_at = (
            parse_local_date(start.get("date"), self.timezone)
            if all_day
            else parse_datetime(start.get("dateTime"))
        )
        if not start_at:
            return None
        end_at = (
            parse_local_date(end.get("date"), self.timezone)
            if all_day
            else parse_datetime(end.get("dateTime"))
        ) or start_at + (timedelta(days=1) if all_day else timedelta(hours=1))
        minutes = (start_at - now).total_seconds() / 60
        attendees = item.get("attendees") or []
        if not isinstance(attendees, list) or not all(isinstance(person, Mapping) for person in attendees):
            raise ConnectorError("Calendar event attendees must be a list")
        self_attendee = next((person for person in attendees if person.get("self")), {})
        needs_action = self_attendee.get("responseStatus") == "needsAction"
        summary = " ".join(str(item.get("summary") or "Meeting").split())
        readiness_keywords = getattr(self, "readiness_keywords", DEFAULT_READINESS_KEYWORDS)
        readiness_minutes = getattr(self, "readiness_minutes", 30.0)
        readiness = (
            not all_day
            and 0 < minutes <= readiness_minutes
            and any(keyword in summary.casefold() for keyword in readiness_keywords)
        )
        if all_day:
            priority, urgency = 58, "normal"
        elif minutes <= 5:
            priority, urgency = 94, "critical"
        elif minutes <= 15:
            priority, urgency = 84, "high"
        elif minutes <= 60:
            priority, urgency = 68, "normal"
        else:
            priority, urgency = 50, "normal"
        if readiness:
            priority, urgency = max(priority, 80), "high"
        prefix = "RSVP " if needs_action else "PREP " if readiness else ""
        return Event(
            id=f"calendar:{event_id}:{start_at.isoformat()}",
            source="calendar",
            kind="calendar_all_day" if all_day else "meeting",
            title=f"{prefix}{summary}",
            body=str(item.get("location") or ""),
            priority=min(100, priority + (6 if needs_action else 0)),
            action_required=needs_action or readiness or (not all_day and minutes <= 15),
            urgency=urgency,
            impact="high" if readiness or (not all_day and minutes <= 15) else "medium",
            occurred_at=parse_datetime(item.get("updated"), default=now) or now,
            due_at=None if all_day else start_at,
            expires_at=end_at,
            dedupe_key=f"calendar:{event_id}:{start_at.isoformat()}",
            url=str(item.get("htmlLink") or ""),
            metadata={
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
                "location": item.get("location"),
                "all_day": all_day,
                "readiness": readiness,
            },
        )
