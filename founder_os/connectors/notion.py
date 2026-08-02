"""Read-only Notion search connector for decisions, deliverables, and evidence."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Mapping

from founder_os.connectors.base import Connector, ConnectorConfigurationError, ConnectorError, configured_secret
from founder_os.connectors.http_client import request_json
from founder_os.models import Event, parse_datetime


class NotionConnector(Connector):
    name = "notion"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.token = configured_secret(config, "token_env", self.secrets)
        self.endpoint = str(config.get("endpoint", "https://api.notion.com/v1")).rstrip("/")
        self.notion_version = str(config.get("notion_version", "2022-06-28"))
        self.page_size = min(100, max(1, int(config.get("page_size", 50))))
        self.max_pages = min(20, max(1, int(config.get("max_pages", 5))))
        self.lookback_days = max(1.0, float(config.get("lookback_days", 30)))
        self.database_ids = {str(value) for value in config.get("database_ids", []) if str(value)}
        if self.database_ids and not all(
            value and all(character.isalnum() or character == "-" for character in value)
            for value in self.database_ids
        ):
            raise ConnectorConfigurationError("notion.database_ids contains an invalid id")
        if not self.database_ids and not bool(config.get("allow_all_shared_pages", False)):
            raise ConnectorConfigurationError(
                "notion.database_ids is required unless allow_all_shared_pages is explicitly enabled"
            )
        keywords = config.get(
            "action_keywords",
            ["decision", "approve", "approval", "review", "sign-off", "décision", "valider", "validation", "à revoir"],
        )
        if not isinstance(keywords, list):
            raise ConnectorConfigurationError("notion.action_keywords must be a list")
        self.action_keywords = tuple(str(value).strip().casefold() for value in keywords if str(value).strip())
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 6)))

    def poll(self, now: datetime) -> list[Event]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        cursor = ""
        pages: list[Mapping[str, Any]] = []
        for _ in range(self.max_pages):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Notion poll exceeded {self.poll_timeout_seconds:.0f} seconds")
            body: dict[str, Any] = {
                "page_size": self.page_size,
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
                "filter": {"property": "object", "value": "page"},
            }
            if cursor:
                body["start_cursor"] = cursor
            payload = request_json(
                f"{self.endpoint}/search",
                method="POST",
                body=body,
                headers={"Authorization": f"Bearer {self.token}", "Notion-Version": self.notion_version},
                timeout=min(self.request_timeout, remaining),
                retries=0,
                deadline_monotonic=deadline,
            )
            results = payload.get("results") or []
            if not isinstance(results, list) or not all(isinstance(item, Mapping) for item in results):
                raise ConnectorError("Notion search response did not contain a result list")
            pages.extend(
                item
                for item in results
                if not self.database_ids
                or str((item.get("parent") or {}).get("database_id") or "") in self.database_ids
            )
            if not payload.get("has_more"):
                break
            next_cursor = str(payload.get("next_cursor") or "")
            if not next_cursor or next_cursor == cursor:
                raise ConnectorError("Notion pagination returned an invalid cursor")
            cursor = next_cursor
        cutoff = now - timedelta(days=self.lookback_days)
        events = [self._normalize(page, now) for page in pages]
        return [event for event in events if event.occurred_at >= cutoff]

    def _normalize(self, page: Mapping[str, Any], now: datetime) -> Event:
        page_id = str(page.get("id") or "").strip()
        if not page_id:
            raise ConnectorError("Notion page is missing its id")
        parent = page.get("parent") or {}
        database_id = str(parent.get("database_id") or "") if isinstance(parent, Mapping) else ""
        properties = page.get("properties") or {}
        if not isinstance(properties, Mapping):
            raise ConnectorError("Notion page properties must be an object")
        title = _notion_title(properties) or "Untitled Notion page"
        status = _notion_status(properties)
        owner = _notion_people(properties)
        due_at = _notion_date(properties)
        project = _notion_text_property(properties, ("Project", "Projet", "Initiative"))
        customer = _notion_text_property(properties, ("Customer", "Client", "Account", "Compte"))
        decision = _notion_text_property(properties, ("Decision", "Décision", "Decision ID"))
        tags = _notion_tags(properties)
        text = " ".join((title, status, " ".join(tags))).casefold()
        closed = status.casefold() in {
            "done", "closed", "complete", "completed", "approved", "validated", "accepted",
            "terminé", "validé", "approuvé", "accepté",
        }
        action = not closed and any(keyword in text for keyword in self.action_keywords)
        gate_status: dict[str, str] = {}
        if closed:
            gate_status["decision"] = "satisfied"
            gate_status["validation"] = "satisfied"
        evidence_categories = ["document"]
        if closed and any(value in text for value in ("decision", "décision", "approved", "validé", "approuvé")):
            evidence_categories.append("validation")
        occurred_at = parse_datetime(page.get("last_edited_time"), default=now) or now
        return Event(
            id=f"notion:{page_id}",
            source="notion",
            title=title,
            body=" · ".join(value for value in (status, owner, project, customer) if value),
            kind="decision" if action else "information",
            priority=76 if action else 42,
            action_required=action,
            urgency="high" if action and due_at and due_at <= now + timedelta(days=1) else "normal",
            impact="high" if customer else "medium",
            occurred_at=occurred_at,
            due_at=due_at,
            expires_at=now + timedelta(days=30),
            dedupe_key=f"notion:{page_id}",
            url=str(page.get("url") or ""),
            metadata={
                "database_id": database_id,
                "project": project,
                "customer": customer,
                "owner": owner,
                "status": status,
                "tags": tags,
                "decision": decision or (title if closed else ""),
                "obligation_type": "decision",
                "gate_status": gate_status,
                "evidence_categories": evidence_categories,
                "evidence_status": "present",
            },
        )


def _notion_title(properties: Mapping[str, Any]) -> str:
    for value in properties.values():
        if not isinstance(value, Mapping) or value.get("type") != "title":
            continue
        title = value.get("title") or []
        return "".join(str(item.get("plain_text") or "") for item in title if isinstance(item, Mapping)).strip()
    return ""


def _notion_status(properties: Mapping[str, Any]) -> str:
    for name in ("Status", "Statut", "State", "État"):
        value = properties.get(name)
        if not isinstance(value, Mapping):
            continue
        inner = value.get("status") or value.get("select") or {}
        if isinstance(inner, Mapping) and inner.get("name"):
            return str(inner["name"])
    return ""


def _notion_people(properties: Mapping[str, Any]) -> str:
    for name in ("Owner", "Assignee", "Responsable", "Propriétaire"):
        value = properties.get(name)
        people = value.get("people") if isinstance(value, Mapping) else None
        if isinstance(people, list):
            names = [str(person.get("name") or "") for person in people if isinstance(person, Mapping)]
            return ", ".join(name for name in names if name)
    return ""


def _notion_date(properties: Mapping[str, Any]) -> datetime | None:
    for name in ("Due", "Deadline", "Échéance", "Date"):
        value = properties.get(name)
        date = value.get("date") if isinstance(value, Mapping) else None
        if isinstance(date, Mapping) and date.get("start"):
            return parse_datetime(date.get("start"))
    return None


def _notion_text_property(properties: Mapping[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = properties.get(name)
        if not isinstance(value, Mapping):
            continue
        rich = value.get("rich_text") or value.get("title") or []
        if isinstance(rich, list):
            text = "".join(str(item.get("plain_text") or "") for item in rich if isinstance(item, Mapping)).strip()
            if text:
                return text
        selected = value.get("select") or {}
        if isinstance(selected, Mapping) and selected.get("name"):
            return str(selected["name"])
    return ""


def _notion_tags(properties: Mapping[str, Any]) -> list[str]:
    for name in ("Tags", "Labels", "Étiquettes"):
        value = properties.get(name)
        selected = value.get("multi_select") if isinstance(value, Mapping) else None
        if isinstance(selected, list):
            return [str(item.get("name") or "") for item in selected if isinstance(item, Mapping) and item.get("name")]
    return []
