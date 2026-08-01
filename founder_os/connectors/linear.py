"""Linear GraphQL connector for personal work and governed team portfolios."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Mapping

from founder_os.connectors.base import Connector, ConnectorConfigurationError, ConnectorError
from founder_os.connectors.http_client import ConnectorHTTPError, request_json
from founder_os.connectors.linear_oauth import LinearAccessTokenProvider
from founder_os.models import Event, parse_datetime, parse_local_date


ASSIGNED_QUERY = """
query FounderOSAssignedIssues($first: Int!, $after: String) {
  viewer {
    id
    assignedIssues(first: $first, after: $after, orderBy: updatedAt, filter: { state: { type: { nin: ["completed", "canceled"] } } }) {
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
        assignee { id name }
        project { id name url }
        labels { nodes { name } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


PORTFOLIO_QUERY = """
query FounderOSPortfolioIssues($first: Int!, $after: String, $teamKeys: [String!]!) {
  viewer { id }
  issues(
    first: $first
    after: $after
    orderBy: updatedAt
    filter: {
      team: { key: { in: $teamKeys } }
      state: { type: { nin: ["completed", "canceled"] } }
    }
  ) {
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
      assignee { id name }
      project { id name url }
      labels { nodes { name } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class LinearConnector(Connector):
    name = "linear"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.auth_scheme = str(config.get("auth_scheme", "api_key")).strip().lower()
        if self.auth_scheme not in {"api_key", "bearer"}:
            raise ConnectorConfigurationError("linear.auth_scheme must be api_key or bearer")
        self.token_provider = LinearAccessTokenProvider(config, secrets=self.secrets)
        self.endpoint = str(config.get("endpoint", "https://api.linear.app/graphql"))
        self.team_keys = {str(value).upper() for value in config.get("team_keys", [])}
        self.scope = str(config.get("scope", "assigned")).strip().lower()
        if self.scope not in {"assigned", "portfolio"}:
            raise ConnectorConfigurationError("linear.scope must be assigned or portfolio")
        if self.scope == "portfolio" and not self.team_keys:
            raise ConnectorConfigurationError("linear.team_keys is required for portfolio scope")
        self.page_size = min(100, max(1, int(config.get("page_size", config.get("limit", 50)))))
        self.max_issues = min(500, max(self.page_size, int(config.get("max_issues", 200))))
        self.max_pages = min(50, max(1, int(config.get("max_pages", 20))))
        self.portfolio_priority_ceiling = min(4, max(1, int(config.get("portfolio_priority_ceiling", 2))))
        self.portfolio_due_horizon = timedelta(days=max(1.0, float(config.get("portfolio_due_horizon_days", 14))))
        self.rollup_projects = bool(config.get("rollup_projects", True))
        self.timezone = str(config.get("timezone", "Europe/Madrid"))
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 6)))
        self.http_retries = min(2, max(0, int(config.get("http_retries", 1))))
        self.active_event_ttl = timedelta(hours=max(1.0, float(config.get("active_event_ttl_hours", 48))))

    def poll(self, now: datetime) -> list[Event]:
        nodes, viewer_id = self._fetch_issues(now)
        events: list[Event] = []
        for issue in nodes:
            team_key = str((issue.get("team") or {}).get("key", "")).upper()
            if self.team_keys and team_key not in self.team_keys:
                continue
            if self.scope == "portfolio" and not self._portfolio_relevant(issue, viewer_id, now):
                continue
            events.append(self._normalize(issue, now, viewer_id=viewer_id))
        if self.scope == "portfolio" and self.rollup_projects:
            return self._rollup_project_events(events, now)
        return events

    def _fetch_issues(self, now: datetime) -> tuple[list[Mapping[str, Any]], str]:
        issues: list[Mapping[str, Any]] = []
        cursor = ""
        viewer_id = ""
        deadline = time.monotonic() + self.poll_timeout_seconds
        page_count = 0
        while len(issues) < self.max_issues:
            if page_count >= self.max_pages:
                raise ConnectorError(f"Linear pagination exceeded {self.max_pages} pages")
            variables = self._page_variables(cursor, len(issues))
            payload = self._graphql(self._query(), variables, deadline, now=now)
            page_count += 1
            nodes, page_info, page_viewer_id = self._page(payload)
            viewer_id = viewer_id or page_viewer_id
            issues.extend(nodes)
            cursor = self._next_cursor(page_info, cursor)
            if not cursor:
                break
        return issues, viewer_id

    def _query(self) -> str:
        return PORTFOLIO_QUERY if self.scope == "portfolio" else ASSIGNED_QUERY

    def _page_variables(self, cursor: str, count: int) -> dict[str, Any]:
        variables: dict[str, Any] = {
            "first": min(self.page_size, self.max_issues - count),
            "after": cursor or None,
        }
        if self.scope == "portfolio":
            variables["teamKeys"] = sorted(self.team_keys)
        return variables

    def _graphql(
        self,
        query: str,
        variables: Mapping[str, Any],
        deadline: float,
        *,
        now: datetime,
    ) -> Mapping[str, Any]:
        timeout = self._remaining_timeout(deadline, retries=self.http_retries)
        try:
            return request_json(
                self.endpoint,
                method="POST",
                body={"query": query, "variables": dict(variables)},
                headers={
                    "Authorization": self.token_provider.authorization(
                        now,
                        deadline_monotonic=deadline,
                    )
                },
                timeout=timeout,
                retries=self.http_retries,
                deadline_monotonic=deadline,
            )
        except ConnectorHTTPError as exc:
            if exc.status_code != 401 or not self.token_provider.refreshable:
                raise
        self.token_provider.invalidate()
        return request_json(
            self.endpoint,
            method="POST",
            body={"query": query, "variables": dict(variables)},
            headers={
                "Authorization": self.token_provider.authorization(
                    now,
                    deadline_monotonic=deadline,
                )
            },
            timeout=self._remaining_timeout(deadline, retries=0),
            retries=0,
            deadline_monotonic=deadline,
        )

    def _remaining_timeout(self, deadline: float, *, retries: int) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Linear poll exceeded {self.poll_timeout_seconds:.0f} seconds")
        return min(self.request_timeout, remaining / (max(0, int(retries)) + 1))

    def _page(self, payload: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], Mapping[str, Any], str]:
        errors = payload.get("errors")
        if errors:
            raise ConnectorError("Linear GraphQL error: " + self._error_summary(errors))
        data = payload.get("data") or {}
        if not isinstance(data, Mapping):
            raise ConnectorError("Linear response data must be an object")
        viewer = data.get("viewer") or {}
        if not isinstance(viewer, Mapping):
            raise ConnectorError("Linear response viewer must be an object")
        connection = data.get("issues") if self.scope == "portfolio" else viewer.get("assignedIssues")
        if not isinstance(connection, Mapping):
            raise ConnectorError("Linear response did not contain an issue connection")
        nodes = connection.get("nodes") or []
        if not isinstance(nodes, list) or not all(isinstance(issue, Mapping) for issue in nodes):
            raise ConnectorError("Linear response did not contain an issue list")
        page_info = connection.get("pageInfo") or {}
        if not isinstance(page_info, Mapping):
            raise ConnectorError("Linear pageInfo must be an object")
        return nodes, page_info, str(viewer.get("id") or "")

    @staticmethod
    def _error_summary(errors: Any) -> str:
        if not isinstance(errors, list):
            return "unknown error"
        codes = []
        for item in errors:
            if not isinstance(item, Mapping):
                continue
            extensions = item.get("extensions") or {}
            code = str(extensions.get("code") or "graphql_error") if isinstance(extensions, Mapping) else "graphql_error"
            codes.append(code if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code) else "graphql_error")
        return "; ".join(codes[:8]) or "unknown error"

    @staticmethod
    def _next_cursor(page_info: Mapping[str, Any], current: str) -> str:
        if not page_info.get("hasNextPage"):
            return ""
        cursor = str(page_info.get("endCursor") or "")
        if not cursor or cursor == current:
            raise ConnectorError("Linear pagination returned an invalid cursor")
        return cursor

    def _portfolio_relevant(self, issue: Mapping[str, Any], viewer_id: str, now: datetime) -> bool:
        priority = self._linear_priority(issue)
        assignee_id = str((issue.get("assignee") or {}).get("id") or "")
        due_at = parse_local_date(issue.get("dueDate"), self.timezone, end_of_day=True)
        due_soon = bool(due_at and due_at <= now + self.portfolio_due_horizon)
        return self._is_blocked(issue) or 0 < priority <= self.portfolio_priority_ceiling or due_soon or bool(viewer_id and assignee_id == viewer_id)

    @staticmethod
    def _linear_priority(issue: Mapping[str, Any]) -> int:
        try:
            return int(issue.get("priority") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _is_blocked(issue: Mapping[str, Any]) -> bool:
        labels = ((issue.get("labels") or {}).get("nodes") or [])
        label_text = " ".join(str(item.get("name") or "") for item in labels if isinstance(item, Mapping))
        state_name = str((issue.get("state") or {}).get("name") or "")
        folded = (label_text + " " + state_name).casefold()
        if any(value in folded for value in ("unblocked", "not blocked", "débloqué", "non bloqué")):
            return False
        tokens = set(re.findall(r"[^\W\d_]+", folded, flags=re.UNICODE))
        return bool(tokens.intersection({"block", "blocked", "blocker", "blocking", "bloqué", "bloquée", "bloquant"}))

    def _normalize(self, issue: Mapping[str, Any], now: datetime, *, viewer_id: str = "") -> Event:
        issue_id = str(issue.get("id") or "").strip()
        issue_title = str(issue.get("title") or "").strip()
        if not issue_id or not issue_title:
            raise ConnectorError("Linear issue is missing id or title")
        labels = [str(item.get("name", "")) for item in ((issue.get("labels") or {}).get("nodes") or [])]
        state_name = str((issue.get("state") or {}).get("name", ""))
        blocker = self._is_blocked(issue)
        linear_priority = self._linear_priority(issue)
        base_priority = {1: 92, 2: 80, 3: 64, 4: 45}.get(linear_priority, 52)
        due_at = parse_local_date(issue.get("dueDate"), self.timezone, end_of_day=True)
        identifier = str(issue.get("identifier") or "ISSUE")
        title = f"{identifier} {issue_title}".strip()
        assignee = issue.get("assignee") or {}
        project = issue.get("project") or {}
        owner_name = str(assignee.get("name") or "Unassigned")
        body = " · ".join(value for value in (state_name, owner_name, str(project.get("name") or "")) if value)
        return Event(
            id=f"linear:{issue_id}",
            source="linear",
            kind="blocker" if blocker else "deadline" if due_at else "information",
            title=title,
            body=body,
            priority=min(100, base_priority + (8 if blocker else 0)),
            action_required=True,
            urgency="critical" if blocker or linear_priority == 1 else "high" if linear_priority == 2 else "normal",
            impact="high" if blocker or linear_priority <= 2 else "medium",
            occurred_at=parse_datetime(issue.get("updatedAt"), default=now) or now,
            due_at=due_at,
            expires_at=now + self.active_event_ttl,
            dedupe_key=f"linear:{issue_id}",
            url=str(issue.get("url") or ""),
            metadata={
                "identifier": identifier,
                "state": state_name,
                "labels": labels,
                "due_date": issue.get("dueDate"),
                "team_key": (issue.get("team") or {}).get("key"),
                "assignee_id": assignee.get("id"),
                "assignee": assignee.get("name"),
                "assigned_to_viewer": bool(viewer_id and str(assignee.get("id") or "") == viewer_id),
                "project_id": project.get("id"),
                "project": project.get("name"),
                "project_url": project.get("url"),
                "rollup": False,
            },
        )

    def _rollup_project_events(self, events: list[Event], now: datetime) -> list[Event]:
        grouped: dict[str, list[Event]] = {}
        standalone: list[Event] = []
        for event in events:
            project_key = str(event.metadata.get("project_id") or event.metadata.get("project") or "")
            if project_key:
                grouped.setdefault(project_key, []).append(event)
            else:
                standalone.append(event)
        for project_key, project_events in grouped.items():
            if len(project_events) < 2:
                standalone.extend(project_events)
            else:
                standalone.append(self._project_rollup(project_key, project_events, now))
        return standalone

    def _project_rollup(self, project_key: str, events: list[Event], now: datetime) -> Event:
        first = events[0]
        project_name = str(first.metadata.get("project") or "Project")
        blockers = [event for event in events if event.kind == "blocker"]
        owners = sorted({str(event.metadata.get("assignee") or "Unassigned") for event in events})
        deadlines = [event.due_at for event in events if event.due_at]
        due_at = min(deadlines) if deadlines else None
        critical = bool(blockers or any(event.urgency == "critical" for event in events))
        digest = sha256(project_key.encode("utf-8")).hexdigest()[:16]
        issue_word = "risk" if len(events) == 1 else "risks"
        return Event(
            id=f"linear:project:{digest}",
            source="linear",
            kind="blocker" if blockers else "deadline" if due_at else "information",
            title=f"{project_name}: {len(events)} open {issue_word}",
            body=f"{len(blockers)} blockers · {len(owners)} owners",
            priority=min(100, max(event.priority for event in events) + 4),
            action_required=True,
            urgency="critical" if critical else "high",
            impact="high",
            occurred_at=max(event.occurred_at for event in events),
            due_at=due_at,
            expires_at=now + self.active_event_ttl,
            dedupe_key=f"linear:project:{digest}",
            url=str(first.metadata.get("project_url") or first.url),
            metadata={
                "rollup": True,
                "project_id": first.metadata.get("project_id"),
                "project": project_name,
                "issue_count": len(events),
                "blocker_count": len(blockers),
                "owners": owners,
                "issue_ids": [event.id for event in events[:50]],
                "issues_truncated": len(events) > 50,
            },
        )
