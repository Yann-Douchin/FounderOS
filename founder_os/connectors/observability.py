"""Read-only Sentry and PostHog evidence connectors."""

from __future__ import annotations

import operator
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from founder_os.connectors.base import Connector, ConnectorConfigurationError, ConnectorError, configured_secret
from founder_os.connectors.http_client import request_json
from founder_os.models import Event, parse_datetime


class SentryConnector(Connector):
    name = "sentry"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.token = configured_secret(config, "token_env", self.secrets)
        self.endpoint = str(config.get("endpoint", "https://sentry.io/api/0")).rstrip("/")
        self.organization = str(config.get("organization") or "").strip()
        self.projects = [str(value).strip() for value in config.get("projects", []) if str(value).strip()]
        if not self.organization or not self.projects:
            raise ConnectorConfigurationError("sentry.organization and sentry.projects are required")
        self.query = str(config.get("query", "is:unresolved"))
        self.page_size = min(100, max(1, int(config.get("page_size", 50))))
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 6)))

    def poll(self, now: datetime) -> list[Event]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        events: list[Event] = []
        for project in self.projects:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Sentry poll exceeded {self.poll_timeout_seconds:.0f} seconds")
            organization = urllib.parse.quote(self.organization, safe="")
            project_slug = urllib.parse.quote(project, safe="")
            payload = request_json(
                f"{self.endpoint}/projects/{organization}/{project_slug}/issues/",
                query={"query": self.query, "limit": self.page_size, "sort": "freq"},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=min(self.request_timeout, remaining),
                retries=0,
                deadline_monotonic=deadline,
                root="array",
            )
            rows = payload
            if not isinstance(rows, list) or not all(isinstance(item, Mapping) for item in rows):
                raise ConnectorError("Sentry response did not contain an issue list")
            events.extend(self._normalize(project, item, now) for item in rows)
        return events

    @staticmethod
    def _normalize(project: str, item: Mapping[str, Any], now: datetime) -> Event:
        issue_id = str(item.get("id") or "").strip()
        if not issue_id:
            raise ConnectorError("Sentry issue is missing its id")
        level = str(item.get("level") or "error").casefold()
        count = _integer(item.get("count"), default=1)
        users = _integer(item.get("userCount"), default=0)
        priority = min(100, 72 + (12 if level in {"fatal", "error"} else 0) + min(12, count // 10))
        title = str(item.get("title") or item.get("culprit") or "Sentry issue")
        return Event(
            id=f"sentry:{issue_id}",
            source="sentry",
            title=f"{project}: {title}",
            body=f"{count} events · {users} users",
            kind="incident",
            priority=priority,
            action_required=True,
            urgency="critical" if level == "fatal" or users > 10 else "high",
            impact="high" if users else "medium",
            occurred_at=parse_datetime(item.get("lastSeen"), default=now) or now,
            expires_at=now + timedelta(days=7),
            dedupe_key=f"sentry:{issue_id}",
            url=str(item.get("permalink") or ""),
            metadata={
                "project": project,
                "level": level,
                "event_count": count,
                "user_count": users,
                "gate_status": {"validation": "blocked"},
                "evidence_status": "failed",
            },
        )


class PostHogConnector(Connector):
    name = "posthog"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.token = configured_secret(config, "token_env", self.secrets)
        self.endpoint = str(config.get("endpoint", "https://us.posthog.com/api")).rstrip("/")
        self.project_id = str(config.get("project_id") or "").strip()
        checks = config.get("checks", [])
        if not self.project_id or not isinstance(checks, list) or not checks or not all(isinstance(item, Mapping) for item in checks):
            raise ConnectorConfigurationError("posthog.project_id and posthog.checks are required")
        self.checks = [dict(item) for item in checks]
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 8)))

    def poll(self, now: datetime) -> list[Event]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        events: list[Event] = []
        project = urllib.parse.quote(self.project_id, safe="")
        for index, check in enumerate(self.checks):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"PostHog poll exceeded {self.poll_timeout_seconds:.0f} seconds")
            query = check.get("query")
            if not isinstance(query, Mapping):
                raise ConnectorConfigurationError("each posthog check requires a query object")
            payload = request_json(
                f"{self.endpoint}/projects/{project}/query/",
                method="POST",
                body={"query": dict(query)},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=min(self.request_timeout, remaining),
                retries=0,
                deadline_monotonic=deadline,
            )
            value = _posthog_value(payload, check)
            threshold = float(check.get("threshold", 0))
            comparator_name = str(check.get("comparator", "gte"))
            comparator = _COMPARATORS.get(comparator_name)
            if comparator is None:
                raise ConnectorConfigurationError(f"unsupported PostHog comparator: {comparator_name}")
            breached = comparator(value, threshold)
            name = str(check.get("name") or f"Check {index + 1}")
            project_name = str(check.get("project") or check.get("entity") or name)
            events.append(Event(
                id=f"posthog:{self.project_id}:{index}",
                source="posthog",
                title=f"{name}: {value:g}",
                body=f"threshold {comparator_name} {threshold:g}",
                kind="incident" if breached else "information",
                priority=86 if breached else 32,
                action_required=breached,
                urgency="high" if breached else "normal",
                impact="high",
                occurred_at=now,
                expires_at=now + timedelta(hours=2),
                dedupe_key=f"posthog:{self.project_id}:{index}",
                url=str(check.get("url") or ""),
                metadata={
                    "project": project_name,
                    "metric": name,
                    "value": value,
                    "threshold": threshold,
                    "breached": breached,
                    "gate_status": {"validation": "blocked"} if breached else {},
                    "evidence_categories": ["analytics"] if not breached else [],
                    "evidence_status": "failed" if breached else "present",
                },
            ))
        return events


_COMPARATORS: Mapping[str, Callable[[float, float], bool]] = {
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "eq": operator.eq,
}


def _posthog_value(payload: Mapping[str, Any], check: Mapping[str, Any]) -> float:
    path = check.get("value_path") or ["results", 0, 0]
    if not isinstance(path, list):
        raise ConnectorConfigurationError("posthog check value_path must be a list")
    value: Any = payload
    try:
        for key in path:
            value = value[int(key)] if isinstance(value, list) else value[str(key)]
        return float(value)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ConnectorError("PostHog response did not contain the configured numeric value") from exc


def _integer(value: Any, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default
