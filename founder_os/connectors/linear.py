"""Linear GraphQL connector for active issues assigned to the authenticated user."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from founder_os.connectors.base import Connector, ConnectorError, configured_secret
from founder_os.connectors.http import request_json
from founder_os.models import Event, parse_datetime


QUERY = """
query FounderOSAssignedIssues($first: Int!) {
  viewer {
    assignedIssues(first: $first, filter: { state: { type: { nin: ["completed", "canceled"] } } }) {
      nodes {
        id
        identifier
        title
        priority
        dueDate
        updatedAt
        url
        state { name type }
        team { key }
        labels { nodes { name } }
      }
    }
  }
}
"""


class LinearConnector(Connector):
    name = "linear"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.token = configured_secret(config, "token_env")
        self.endpoint = str(config.get("endpoint", "https://api.linear.app/graphql"))
        self.team_keys = {str(value).upper() for value in config.get("team_keys", [])}
        self.limit = min(100, max(1, int(config.get("limit", 50))))

    def poll(self, now: datetime) -> list[Event]:
        payload = request_json(
            self.endpoint,
            method="POST",
            body={"query": QUERY, "variables": {"first": self.limit}},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if payload.get("errors"):
            raise ConnectorError(f"Linear GraphQL error: {payload['errors']!r}")
        nodes = (((payload.get("data") or {}).get("viewer") or {}).get("assignedIssues") or {}).get("nodes") or []
        events = []
        for issue in nodes:
            team_key = str((issue.get("team") or {}).get("key", "")).upper()
            if self.team_keys and team_key not in self.team_keys:
                continue
            events.append(self._normalize(issue, now))
        return events

    @staticmethod
    def _normalize(issue: Mapping[str, Any], now: datetime) -> Event:
        labels = [str(item.get("name", "")) for item in ((issue.get("labels") or {}).get("nodes") or [])]
        label_text = " ".join(labels).casefold()
        state_name = str((issue.get("state") or {}).get("name", ""))
        blocker = "block" in label_text or "blocked" in state_name.casefold()
        linear_priority = int(issue.get("priority") or 0)
        base_priority = {1: 92, 2: 80, 3: 64, 4: 45}.get(linear_priority, 52)
        due_at = parse_datetime(issue.get("dueDate"))
        identifier = str(issue.get("identifier") or "ISSUE")
        title = f"{identifier} {issue.get('title', '')}".strip()
        return Event(
            id=f"linear:{issue.get('id')}",
            source="linear",
            kind="blocker" if blocker else "deadline" if due_at else "information",
            title=title,
            body=state_name,
            priority=min(100, base_priority + (8 if blocker else 0)),
            action_required=True,
            urgency="critical" if blocker else "high" if linear_priority == 1 else "normal",
            impact="high" if blocker or linear_priority <= 2 else "medium",
            occurred_at=parse_datetime(issue.get("updatedAt"), default=now) or now,
            due_at=due_at,
            expires_at=(due_at + timedelta(hours=12)) if due_at else now + timedelta(hours=24),
            dedupe_key=f"linear:{issue.get('id')}",
            url=str(issue.get("url") or ""),
            metadata={"identifier": identifier, "state": state_name, "labels": labels},
        )
