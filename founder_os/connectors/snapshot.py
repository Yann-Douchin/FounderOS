"""Read normalized connector snapshots produced by an authorized local bridge."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from founder_os.connectors.base import (
    Connector,
    ConnectorConfigurationError,
    ConnectorError,
    ConnectorStaleError,
    ConnectorUnavailableError,
)
from founder_os.models import Event, parse_datetime


SCHEMA_VERSION = 1


class JsonSnapshotConnector(Connector):
    """Load a complete source snapshot without exposing its OAuth credentials.

    The intended writer is an authorized local bridge, such as a Codex session
    with a connected Linear, Slack, Gmail, or Google Calendar app. Snapshot
    files live under the private FounderOS state directory, outside Git.
    """

    def __init__(self, config: Mapping[str, Any], *, source: str) -> None:
        super().__init__(config)
        self.name = source
        self.source = source
        raw_path = str(config.get("snapshot_path", "")).strip()
        if not raw_path:
            raise ConnectorConfigurationError(f"{source}.snapshot_path is required in snapshot mode")
        self.path = Path(raw_path).expanduser()
        self.max_snapshot_age = timedelta(
            minutes=max(1.0, float(config.get("max_snapshot_age_minutes", 1440)))
        )
        self.default_event_ttl = timedelta(
            minutes=max(1.0, float(config.get("default_event_ttl_minutes", 1440)))
        )

    def poll(self, now: datetime) -> list[Event]:
        if not self.path.exists():
            raise ConnectorUnavailableError(f"{self.source} snapshot is missing: {self.path}")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorError(f"cannot read {self.source} snapshot {self.path}: {exc}") from exc

        rows, generated_at = self._unpack(payload)
        if now - generated_at > self.max_snapshot_age:
            age_minutes = max(0, round((now - generated_at).total_seconds() / 60))
            raise ConnectorStaleError(
                f"{self.source} snapshot is {age_minutes} minutes old, maximum is "
                f"{round(self.max_snapshot_age.total_seconds() / 60)}"
            )

        events: list[Event] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise ConnectorError(f"{self.source} snapshot event {index} must be an object")
            normalized = dict(row)
            normalized.setdefault("occurred_at", generated_at.isoformat())
            if not normalized.get("expires_at"):
                due_at = parse_datetime(normalized.get("due_at"))
                expiry_base = max(generated_at, due_at) if due_at else generated_at
                normalized["expires_at"] = (expiry_base + self.default_event_ttl).isoformat()
            try:
                events.append(Event.from_mapping(normalized, source=self.source))
            except (TypeError, ValueError) as exc:
                raise ConnectorError(f"invalid {self.source} snapshot event {index}: {exc}") from exc
        return events

    def _unpack(self, payload: Any) -> tuple[list[Any], datetime]:
        if isinstance(payload, list):
            rows = payload
            generated_at = parse_datetime(self.path.stat().st_mtime)
            if generated_at is None:
                raise ConnectorError(f"{self.source} snapshot modification time is invalid")
            return rows, generated_at
        if not isinstance(payload, Mapping):
            raise ConnectorError(f"{self.source} snapshot root must be an object or array")
        version = payload.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ConnectorError(
                f"unsupported {self.source} snapshot schema_version {version!r}; expected {SCHEMA_VERSION}"
            )
        envelope_source = str(payload.get("source", self.source)).strip().lower()
        if envelope_source != self.source:
            raise ConnectorError(
                f"snapshot source {envelope_source!r} does not match configured source {self.source!r}"
            )
        rows = payload.get("events")
        if not isinstance(rows, list):
            raise ConnectorError(f"{self.source} snapshot must contain an events array")
        generated_at = parse_datetime(payload.get("generated_at"))
        if generated_at is None:
            generated_at = parse_datetime(self.path.stat().st_mtime)
        if generated_at is None:
            raise ConnectorError(f"{self.source} snapshot generated_at is invalid")
        return rows, generated_at
