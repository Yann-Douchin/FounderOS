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
DEFAULT_AVAILABILITY_KEYWORDS = (
    "out of office", "ooo", "vacation", "annual leave", "travel", "flight",
    "absence", "congé", "congés", "vacances", "voyage", "déplacement",
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
        availability_values = config.get("availability_keywords", DEFAULT_AVAILABILITY_KEYWORDS)
        if not isinstance(availability_values, (list, tuple)):
            raise ConnectorConfigurationError("calendar.availability_keywords must be a list")
        self.availability_keywords = tuple(str(value).strip().casefold() for value in availability_values if str(value).strip())
        owner_map = config.get("availability_owner_map", {})
        if not isinstance(owner_map, Mapping):
            raise ConnectorConfigurationError("calendar.availability_owner_map must be an object")
        self.availability_owner_map = {
            str(key).strip().casefold(): str(value).strip()
            for key, value in owner_map.items()
            if str(key).strip() and str(value).strip()
        }
        self.lookback_hours = max(0.0, float(config.get("lookback_hours", 2)))
        self.followup_hours = max(1.0, float(config.get("followup_hours", 24)))
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
            "timeMin": (now - timedelta(hours=self.lookback_hours)).isoformat(),
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
        response_status = str(self_attendee.get("responseStatus") or "").strip()
        if response_status.casefold() == "declined":
            return None
        needs_action = response_status == "needsAction"
        transparency = str(item.get("transparency") or "opaque").strip().casefold()
        calendar_busy = transparency != "transparent"
        summary = " ".join(str(item.get("summary") or "Meeting").split())
        description = " ".join(str(item.get("description") or "").split())[:1000]
        readiness_keywords = getattr(self, "readiness_keywords", DEFAULT_READINESS_KEYWORDS)
        readiness_minutes = getattr(self, "readiness_minutes", 30.0)
        readiness = (
            not all_day
            and calendar_busy
            and 0 < minutes <= readiness_minutes
            and any(keyword in summary.casefold() for keyword in readiness_keywords)
        )
        important_meeting = any(keyword in summary.casefold() for keyword in readiness_keywords)
        after_meeting = (
            not all_day
            and calendar_busy
            and end_at <= now
            and now - end_at <= timedelta(hours=getattr(self, "followup_hours", 24.0))
            and important_meeting
        )
        availability = all_day and any(
            keyword in summary.casefold()
            for keyword in getattr(self, "availability_keywords", DEFAULT_AVAILABILITY_KEYWORDS)
        )
        extended = item.get("extendedProperties") or {}
        explicit_owner = ""
        if isinstance(extended, Mapping):
            for values in (extended.get("private"), extended.get("shared")):
                if isinstance(values, Mapping) and values.get("founderos_owner"):
                    explicit_owner = str(values["founderos_owner"]).strip()
                    break
        availability_owner = explicit_owner or next(
            (
                owner
                for marker, owner in getattr(self, "availability_owner_map", {}).items()
                if marker in summary.casefold()
            ),
            "self",
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
        prefix = "FOLLOW-UP " if after_meeting else "RSVP " if needs_action else "PREP " if readiness else ""
        phase = "after" if after_meeting else "before" if readiness else "scheduled"
        attendee_domains = sorted({
            str(person.get("email") or "").rsplit("@", 1)[1].casefold()
            for person in attendees
            if "@" in str(person.get("email") or "") and not person.get("self")
        })
        return Event(
            id=f"calendar:{event_id}:{start_at.isoformat()}:{phase}",
            source="calendar",
            kind="calendar_all_day" if all_day else "meeting",
            title=f"{prefix}{summary}",
            body=str(item.get("location") or ""),
            priority=min(100, priority + (6 if needs_action else 0)),
            action_required=(
                needs_action
                or readiness
                or after_meeting
                or (calendar_busy and not all_day and 0 < minutes <= 15)
            ),
            urgency=urgency,
            impact="high" if readiness or (not all_day and minutes <= 15) else "medium",
            occurred_at=parse_datetime(item.get("updated"), default=now) or now,
            due_at=None if all_day else start_at,
            expires_at=(now + timedelta(hours=self.followup_hours)) if after_meeting else end_at,
            dedupe_key=f"calendar:{event_id}:{start_at.isoformat()}:{phase}",
            url=str(item.get("htmlLink") or ""),
            metadata={
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
                "location": item.get("location"),
                "all_day": all_day,
                "calendar_busy": calendar_busy,
                "response_status": response_status,
                "transparency": transparency,
                "event_type": str(item.get("eventType") or "default"),
                "readiness": readiness,
                "meeting_id": event_id,
                "meeting_phase": phase,
                "rsvp_required": needs_action,
                "obligation_type": "decision" if needs_action and phase == "scheduled" else "meeting",
                "description": description,
                "attendee_domains": attendee_domains,
                "relationship_key": attendee_domains[0] if len(attendee_domains) == 1 else "",
                "availability": "unavailable" if availability else "available",
                "person": availability_owner if availability else "",
            },
        )
