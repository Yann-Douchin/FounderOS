"""Read-only Google Drive and Sheets connectors for decisions and proof matrices."""

from __future__ import annotations

import re
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Mapping

from founder_os.connectors.base import Connector, ConnectorConfigurationError, ConnectorError
from founder_os.connectors.google_oauth import GoogleAccessTokenProvider
from founder_os.connectors.http_client import ConnectorHTTPError, request_json
from founder_os.models import Event, parse_datetime


class _GoogleWorkspaceConnector(Connector):
    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.token_provider = GoogleAccessTokenProvider(config, secrets=self.secrets)
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 6)))

    def _request(
        self,
        url: str,
        now: datetime,
        deadline: float,
        *,
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = self.token_provider.token(now, deadline_monotonic=deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{self.name} poll exceeded {self.poll_timeout_seconds:.0f} seconds")
        try:
            return request_json(
                url,
                query=query,
                headers={"Authorization": f"Bearer {token}"},
                timeout=min(self.request_timeout, remaining),
                retries=0,
                deadline_monotonic=deadline,
            )
        except ConnectorHTTPError as exc:
            if exc.status_code != 401 or not self.token_provider.refreshable:
                raise
        self.token_provider.invalidate()
        token = self.token_provider.token(now, deadline_monotonic=deadline)
        return request_json(
            url,
            query=query,
            headers={"Authorization": f"Bearer {token}"},
            timeout=min(self.request_timeout, max(0.1, deadline - time.monotonic())),
            retries=0,
            deadline_monotonic=deadline,
        )


class GoogleDriveConnector(_GoogleWorkspaceConnector):
    name = "drive"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.endpoint = str(config.get("endpoint", "https://www.googleapis.com/drive/v3")).rstrip("/")
        self.lookback_days = max(1.0, float(config.get("lookback_days", 30)))
        self.page_size = min(1000, max(1, int(config.get("page_size", 100))))
        self.max_pages = min(20, max(1, int(config.get("max_pages", 5))))
        self.folder_ids = [str(value) for value in config.get("folder_ids", []) if str(value)]
        if self.folder_ids and not all(
            re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in self.folder_ids
        ):
            raise ConnectorConfigurationError("drive.folder_ids contains an invalid id")
        if not self.folder_ids and not bool(config.get("allow_all_files", False)):
            raise ConnectorConfigurationError(
                "drive.folder_ids is required unless allow_all_files is explicitly enabled"
            )
        values = config.get(
            "action_keywords",
            ["decision", "proposal", "contract", "review", "validation", "décision", "proposition", "contrat"],
        )
        if not isinstance(values, list):
            raise ConnectorConfigurationError("drive.action_keywords must be a list")
        self.action_keywords = tuple(str(value).strip().casefold() for value in values if str(value).strip())

    def poll(self, now: datetime) -> list[Event]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        cutoff = now - timedelta(days=self.lookback_days)
        clauses = ["trashed = false", f"modifiedTime >= '{cutoff.isoformat().replace('+00:00', 'Z')}'"]
        if self.folder_ids:
            clauses.append("(" + " or ".join(f"'{folder}' in parents" for folder in self.folder_ids) + ")")
        page_token = ""
        files: list[Mapping[str, Any]] = []
        for _ in range(self.max_pages):
            payload = self._request(
                f"{self.endpoint}/files",
                now,
                deadline,
                query={
                    "q": " and ".join(clauses),
                    "pageSize": self.page_size,
                    "pageToken": page_token or None,
                    "orderBy": "modifiedTime desc",
                    "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink,owners,parents,properties,description)",
                },
            )
            rows = payload.get("files") or []
            if not isinstance(rows, list) or not all(isinstance(item, Mapping) for item in rows):
                raise ConnectorError("Drive response did not contain a file list")
            files.extend(rows)
            next_token = str(payload.get("nextPageToken") or "")
            if not next_token:
                break
            if next_token == page_token:
                raise ConnectorError("Drive pagination returned an invalid token")
            page_token = next_token
        return [self._normalize(item, now) for item in files]

    def _normalize(self, item: Mapping[str, Any], now: datetime) -> Event:
        file_id = str(item.get("id") or "").strip()
        name = " ".join(str(item.get("name") or "Untitled Drive file").split())
        if not file_id:
            raise ConnectorError("Drive file is missing its id")
        properties = item.get("properties") or {}
        if not isinstance(properties, Mapping):
            properties = {}
        description = " ".join(str(item.get("description") or "").split())[:1000]
        text = f"{name} {description}".casefold()
        action = any(keyword in text for keyword in self.action_keywords) and str(properties.get("founderos_status") or "").casefold() not in {"closed", "done", "approved"}
        owner = ", ".join(
            str(person.get("displayName") or person.get("emailAddress") or "")
            for person in item.get("owners") or []
            if isinstance(person, Mapping)
        )
        project = str(properties.get("founderos_project") or "")
        customer = str(properties.get("founderos_customer") or "")
        decision = str(properties.get("founderos_decision") or "")
        evidence = _evidence_tokens(str(properties.get("founderos_evidence") or "document"))
        categories = list(dict.fromkeys(item["category"] for item in evidence))
        status = str(properties.get("founderos_status") or "")
        gate_status = {"validation": "satisfied"} if status.casefold() in {"approved", "validated", "validé"} else {}
        occurred_at = parse_datetime(item.get("modifiedTime"), default=now) or now
        return Event(
            id=f"drive:{file_id}",
            source="drive",
            title=name,
            body=description,
            kind="decision" if action else "information",
            priority=72 if action else 38,
            action_required=action,
            urgency="high" if action else "normal",
            impact="high" if customer else "medium",
            occurred_at=occurred_at,
            expires_at=now + timedelta(days=30),
            dedupe_key=f"drive:{file_id}",
            url=str(item.get("webViewLink") or ""),
            metadata={
                "mime_type": item.get("mimeType"),
                "owner": owner,
                "project": project,
                "customer": customer,
                "decision": decision,
                "obligation_type": "decision",
                "status": status,
                "gate_status": gate_status,
                "evidence_categories": categories,
                "evidence": evidence,
                "evidence_status": "present",
            },
        )


class GoogleSheetsConnector(_GoogleWorkspaceConnector):
    name = "sheets"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.endpoint = str(config.get("endpoint", "https://sheets.googleapis.com/v4")).rstrip("/")
        spreadsheets = config.get("spreadsheets", [])
        if not isinstance(spreadsheets, list) or not all(isinstance(item, Mapping) for item in spreadsheets):
            raise ConnectorConfigurationError("sheets.spreadsheets must be a list of objects")
        self.spreadsheets = [dict(item) for item in spreadsheets]
        self.max_rows = min(1000, max(1, int(config.get("max_rows", 200))))

    def poll(self, now: datetime) -> list[Event]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        events: list[Event] = []
        for sheet in self.spreadsheets:
            spreadsheet_id = str(sheet.get("id") or "").strip()
            ranges = sheet.get("ranges") or []
            if not spreadsheet_id or not isinstance(ranges, list) or not ranges:
                raise ConnectorConfigurationError("each sheets.spreadsheets entry requires id and ranges")
            quoted = urllib.parse.quote(spreadsheet_id, safe="")
            payload = self._request(
                f"{self.endpoint}/spreadsheets/{quoted}/values:batchGet",
                now,
                deadline,
                query={"ranges": [str(value) for value in ranges], "majorDimension": "ROWS"},
            )
            value_ranges = payload.get("valueRanges") or []
            if not isinstance(value_ranges, list) or not all(isinstance(item, Mapping) for item in value_ranges):
                raise ConnectorError("Sheets response did not contain value ranges")
            for value_range in value_ranges:
                events.extend(self._normalize_range(sheet, value_range, now))
        return events

    def _normalize_range(
        self,
        sheet: Mapping[str, Any],
        value_range: Mapping[str, Any],
        now: datetime,
    ) -> list[Event]:
        values = value_range.get("values") or []
        if not isinstance(values, list) or not values:
            return []
        headers = [str(value).strip() for value in values[0]]
        if not headers:
            return []
        events: list[Event] = []
        spreadsheet_id = str(sheet.get("id") or "")
        range_name = str(value_range.get("range") or "")
        for row_index, values_row in enumerate(values[1 : self.max_rows + 1], start=2):
            if not isinstance(values_row, list):
                continue
            row = {headers[index]: values_row[index] if index < len(values_row) else "" for index in range(len(headers))}
            title = _row_value(row, sheet, "title_column", ("Title", "Task", "Decision", "Item", "Titre"))
            if not title:
                continue
            status = _row_value(row, sheet, "status_column", ("Status", "State", "Statut", "État"))
            owner = _row_value(row, sheet, "owner_column", ("Owner", "Assignee", "Responsable"))
            project = _row_value(row, sheet, "project_column", ("Project", "Projet", "Initiative")) or str(sheet.get("project") or "")
            customer = _row_value(row, sheet, "customer_column", ("Customer", "Client", "Account"))
            decision = _row_value(row, sheet, "decision_column", ("Decision", "Décision", "Decision ID"))
            due_at = parse_datetime(_row_value(row, sheet, "due_column", ("Due", "Deadline", "Échéance")))
            normalized_status = status.casefold()
            closed = normalized_status in {"done", "closed", "complete", "approved", "terminé", "validé"}
            feedback = str(row.get(str(sheet.get("feedback_column") or "Feedback")) or "").strip()
            evidence_specs = _sheet_evidence_specs(sheet.get("evidence_columns", []))
            evidence = [
                {"category": spec["category"], "scope": spec["scope"], "detail": f"{range_name} row {row_index}"}
                for spec in evidence_specs
                if str(row.get(spec["column"]) or "").strip().casefold()
                in {"yes", "true", "1", "ok", "pass", "passed", "oui", "validé"}
            ]
            evidence_categories = list(dict.fromkeys(item["category"] for item in evidence))
            gate_status = {"validation": "satisfied"} if closed else {}
            row_key = str(row.get(str(sheet.get("id_column") or "ID")) or row_index)
            events.append(Event(
                id=f"sheets:{spreadsheet_id}:{range_name}:{row_key}",
                source="sheets",
                title=title,
                body=" · ".join(value for value in (status, owner, feedback) if value),
                kind="feedback" if feedback else "decision" if not closed else "information",
                priority=78 if feedback and not owner else 72 if not closed else 36,
                action_required=not closed,
                urgency="high" if due_at and due_at <= now + timedelta(days=1) else "normal",
                impact="high" if customer else "medium",
                occurred_at=now,
                due_at=due_at,
                expires_at=now + timedelta(days=30),
                dedupe_key=f"sheets:{spreadsheet_id}:{range_name}:{row_key}",
                url=f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}",
                metadata={
                    "project": project,
                    "customer": customer,
                    "decision": decision,
                    "owner": owner,
                    "status": status,
                    "feedback": bool(feedback),
                    "gate_status": gate_status,
                    "evidence_categories": evidence_categories,
                    "evidence": evidence,
                    "evidence_status": "present",
                    "row": row_index,
                    "range": range_name,
                },
            ))
        return events


def _row_value(
    row: Mapping[str, Any],
    config: Mapping[str, Any],
    config_key: str,
    fallbacks: tuple[str, ...],
) -> str:
    configured = str(config.get(config_key) or "").strip()
    names = (configured, *fallbacks) if configured else fallbacks
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def _evidence_tokens(value: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for token in value.split(","):
        category, separator, scope = token.strip().partition(":")
        if category:
            result.append({"category": category.casefold(), "scope": scope.strip() if separator else ""})
    return result


def _sheet_evidence_specs(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list):
        raise ConnectorConfigurationError("sheets evidence_columns must be a list")
    result: list[dict[str, str]] = []
    for value in values:
        if isinstance(value, str):
            column = category = value.strip()
            scope = ""
        elif isinstance(value, Mapping):
            column = str(value.get("column") or "").strip()
            category = str(value.get("category") or column).strip()
            scope = str(value.get("scope") or "").strip()
        else:
            raise ConnectorConfigurationError("sheets evidence column entries must be strings or objects")
        if not column or not category:
            raise ConnectorConfigurationError("sheets evidence column entries require column and category")
        result.append({"column": column, "category": category.casefold(), "scope": scope})
    return result
