"""Read-only GitHub and deployment connectors for delivery gates."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any, Mapping

from founder_os.connectors.base import Connector, ConnectorConfigurationError, ConnectorError, configured_secret
from founder_os.connectors.http_client import request_json
from founder_os.models import Event, parse_datetime


class GitHubConnector(Connector):
    name = "github"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.token = configured_secret(config, "token_env", self.secrets)
        self.endpoint = str(config.get("endpoint", "https://api.github.com")).rstrip("/")
        self.api_version = str(config.get("api_version", "2022-11-28"))
        self.repositories = [str(value).strip() for value in config.get("repositories", []) if str(value).strip()]
        if not self.repositories or not all(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) for value in self.repositories):
            raise ConnectorConfigurationError("github.repositories must contain owner/repository names")
        self.page_size = min(100, max(1, int(config.get("page_size", 50))))
        self.lookback_hours = max(1.0, float(config.get("lookback_hours", 48)))
        self.review_login = str(config.get("review_login") or "").strip()
        deployment_workflows = config.get("deployment_workflows", ["deploy", "release", "production"])
        if not isinstance(deployment_workflows, list):
            raise ConnectorConfigurationError("github.deployment_workflows must be a list")
        self.deployment_workflows = tuple(str(value).strip().casefold() for value in deployment_workflows if str(value).strip())
        project_map = config.get("project_map", {})
        if not isinstance(project_map, Mapping):
            raise ConnectorConfigurationError("github.project_map must be an object")
        self.project_map = {str(key): str(value) for key, value in project_map.items() if str(key) and str(value)}
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 6)))

    def poll(self, now: datetime) -> list[Event]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        events: list[Event] = []
        for repository in self.repositories:
            events.extend(self._workflow_events(repository, now, deadline))
            events.extend(self._pull_request_events(repository, now, deadline))
        return events

    def _workflow_events(self, repository: str, now: datetime, deadline: float) -> list[Event]:
        payload = self._get(
            f"{self.endpoint}/repos/{repository}/actions/runs",
            now,
            deadline,
            query={"per_page": self.page_size},
        )
        runs = payload.get("workflow_runs") or []
        if not isinstance(runs, list) or not all(isinstance(item, Mapping) for item in runs):
            raise ConnectorError("GitHub Actions response did not contain workflow runs")
        cutoff = now - timedelta(hours=self.lookback_hours)
        events: list[Event] = []
        for run in runs:
            updated = parse_datetime(run.get("updated_at"), default=now) or now
            if updated < cutoff:
                continue
            run_id = str(run.get("id") or "")
            if not run_id:
                continue
            status = str(run.get("status") or "")
            conclusion = str(run.get("conclusion") or "")
            failed = conclusion in {"failure", "timed_out", "cancelled", "startup_failure", "action_required"}
            success = conclusion == "success"
            branch = str(run.get("head_branch") or "")
            workflow_name = str(run.get("name") or "workflow")
            deployment_workflow = workflow_name.strip().casefold() in self.deployment_workflows
            project = self.project_map.get(repository, repository)
            events.append(Event(
                id=f"github:run:{run_id}",
                source="github",
                title=f"{repository} {workflow_name}: {conclusion or status}",
                body=branch,
                kind="incident" if failed else "information",
                priority=90 if failed else 34,
                action_required=failed,
                urgency="critical" if failed else "normal",
                impact="high",
                occurred_at=updated,
                expires_at=now + timedelta(days=7),
                dedupe_key=f"github:run:{run_id}",
                url=str(run.get("html_url") or ""),
                metadata={
                    "repository": repository,
                    "project": project,
                    "branch": branch,
                    "workflow_status": status,
                    "workflow_conclusion": conclusion,
                    "deployment_status": "success" if success and deployment_workflow else "failed" if failed and deployment_workflow else "pending",
                    "gate_status": ({"deployment": "satisfied", "code": "satisfied"} if success and deployment_workflow else {"deployment": "blocked"} if failed and deployment_workflow else {"code": "satisfied"} if success else {"code": "blocked"} if failed else {}),
                    "evidence_categories": (["deployment", "code"] if success and deployment_workflow else ["code"] if success else []),
                    "evidence_status": "present" if success else "failed",
                },
            ))
        return events

    def _pull_request_events(self, repository: str, now: datetime, deadline: float) -> list[Event]:
        payload = self._get(
            f"{self.endpoint}/search/issues",
            now,
            deadline,
            query={
                "q": f"repo:{repository} is:pr is:open" + (f" review-requested:{self.review_login}" if self.review_login else ""),
                "per_page": self.page_size,
                "sort": "updated",
                "order": "desc",
            },
        )
        rows = payload.get("items") or []
        if not isinstance(rows, list) or not all(isinstance(item, Mapping) for item in rows):
            raise ConnectorError("GitHub pull response did not contain a pull request list")
        events: list[Event] = []
        for pull in rows:
            number = str(pull.get("number") or "")
            if not number:
                continue
            labels = [str(value.get("name") or "").casefold() for value in pull.get("labels") or [] if isinstance(value, Mapping)]
            review_needed = bool(self.review_login) or any("review" in value for value in labels)
            events.append(Event(
                id=f"github:pr:{repository}:{number}",
                source="github",
                title=f"{repository} PR #{number}: {pull.get('title') or 'Review'}",
                body="review requested" if review_needed else "open pull request",
                kind="decision" if review_needed else "information",
                priority=74 if review_needed else 42,
                action_required=review_needed,
                urgency="high" if review_needed else "normal",
                impact="medium",
                occurred_at=parse_datetime(pull.get("updated_at"), default=now) or now,
                expires_at=now + timedelta(days=14),
                dedupe_key=f"github:pr:{repository}:{number}",
                url=str(pull.get("html_url") or ""),
                metadata={
                    "repository": repository,
                    "project": self.project_map.get(repository, repository),
                    "owner": str((pull.get("user") or {}).get("login") or ""),
                    "gate_status": {"code": "pending"},
                    "evidence_status": "failed" if review_needed else "present",
                },
            ))
        return events

    def _get(
        self,
        url: str,
        now: datetime,
        deadline: float,
        *,
        query: Mapping[str, Any],
    ) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"GitHub poll exceeded {self.poll_timeout_seconds:.0f} seconds")
        return request_json(
            url,
            query=query,
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": self.api_version,
            },
            timeout=min(self.request_timeout, remaining),
            retries=0,
            deadline_monotonic=deadline,
        )


class DeploymentConnector(Connector):
    """Normalize a read-only deployment status endpoint owned by the operator."""

    name = "deployment"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.endpoint = str(config.get("endpoint") or "").strip()
        if not self.endpoint:
            raise ConnectorConfigurationError("deployment.endpoint is required")
        token_name = str(config.get("token_env") or "").strip()
        self.token = self.secrets.get(token_name) if token_name else ""
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 6)))
        self.max_deployments = min(500, max(1, int(config.get("max_deployments", 100))))

    def poll(self, now: datetime) -> list[Event]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = request_json(
            self.endpoint,
            headers=headers,
            timeout=min(self.request_timeout, self.poll_timeout_seconds),
            retries=0,
        )
        rows = payload.get("deployments") or []
        if not isinstance(rows, list) or not all(isinstance(item, Mapping) for item in rows):
            raise ConnectorError("deployment response must contain a deployment list")
        events: list[Event] = []
        for item in rows[: self.max_deployments]:
            deployment_id = str(item.get("id") or "").strip()
            project = str(item.get("project") or item.get("service") or "Deployment")
            status = str(item.get("status") or "unknown").casefold()
            if not deployment_id:
                continue
            success = status in {"success", "succeeded", "ready", "deployed", "live"}
            failed = status in {"failed", "error", "cancelled", "timed_out"}
            events.append(Event(
                id=f"deployment:{deployment_id}",
                source="deployment",
                title=f"{project} deployment {status}",
                kind="incident" if failed else "information",
                priority=92 if failed else 36,
                action_required=failed,
                urgency="critical" if failed else "normal",
                impact="high",
                occurred_at=parse_datetime(item.get("updated_at"), default=now) or now,
                expires_at=now + timedelta(days=7),
                dedupe_key=f"deployment:{deployment_id}",
                url=str(item.get("url") or ""),
                metadata={
                    "project": project,
                    "environment": item.get("environment"),
                    "deployment_status": "success" if success else "failed" if failed else "pending",
                    "gate_status": {"deployment": "satisfied"} if success else {"deployment": "blocked"} if failed else {},
                    "evidence_categories": ["deployment"] if success else [],
                    "evidence_status": "present" if success else "failed" if failed else "stale",
                },
            ))
        return events
