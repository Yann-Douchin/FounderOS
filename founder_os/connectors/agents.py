"""Snapshot connector for agent permissions and live usage windows."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from founder_os.agents.bridge import BridgeStore
from founder_os.agents.codex import CodexAppServerClient, CodexAppServerError, normalize_rate_limits
from founder_os.connectors.base import Connector
from founder_os.models import Event, parse_datetime


class AgentBridgeConnector(Connector):
    emits_snapshot = True

    def __init__(self, config: Mapping[str, Any], *, source: str) -> None:
        super().__init__(config)
        self.name = source
        self.source = source
        self.store = BridgeStore(Path(str(config.get("state_dir", ".data/agents"))))
        usage = config.get("usage")
        self.usage_config = dict(usage) if isinstance(usage, Mapping) else {}
        self.usage_mode = str(self.usage_config.get("mode", "snapshot")).strip().lower()
        self.usage_refresh_seconds = max(
            self.poll_interval_seconds,
            float(self.usage_config.get("refresh_seconds", 60)),
        )
        self.last_usage_error = ""
        self._next_usage_at: datetime | None = None
        self._cached_usage: dict[str, Any] | None = None
        self._codex: CodexAppServerClient | None = None
        if self.source == "chatgpt_codex" and self.usage_mode == "codex_app_server":
            try:
                self._codex = CodexAppServerClient(
                    str(self.usage_config.get("codex_binary", "")),
                    timeout_seconds=float(self.usage_config.get("timeout_seconds", 5)),
                )
            except CodexAppServerError as exc:
                self.last_usage_error = str(exc)

    def poll(self, now: datetime) -> list[Event]:
        events = [self._permission_event(record) for record in self.store.pending_requests(self.source, now=now)]
        usage = self._read_usage(now)
        if usage:
            events.append(self._usage_event(usage))
        return events

    def decide(self, request_id: str, decision: str, *, input_key: str = "") -> bool:
        return self.store.decide(self.source, request_id, decision, input_key=input_key)

    def close(self) -> None:
        if self._codex:
            self._codex.close()

    def _read_usage(self, now: datetime) -> dict[str, Any] | None:
        if self.usage_mode in {"", "disabled", "none"}:
            return None
        if self.usage_mode == "codex_app_server" and self._codex:
            cached_expiry = parse_datetime((self._cached_usage or {}).get("expires_at"))
            if self._next_usage_at and now < self._next_usage_at:
                return self._cached_usage if cached_expiry and cached_expiry > now else None
            self._next_usage_at = now + timedelta(seconds=self.usage_refresh_seconds)
            try:
                result = self._codex.read_rate_limits()
                usage = normalize_rate_limits(
                    result,
                    now=now,
                    ttl_seconds=float(self.usage_config.get("ttl_seconds", 120)),
                )
                self.last_usage_error = ""
                self._cached_usage = usage
                return usage
            except CodexAppServerError as exc:
                self.last_usage_error = str(exc)
                if self._cached_usage and cached_expiry and cached_expiry > now:
                    return self._cached_usage
        return self.store.read_usage(self.source, now=now)

    def _permission_event(self, record: Mapping[str, Any]) -> Event:
        request_id = str(record["request_id"])
        return Event(
            id=f"{self.source}:permission:{request_id}",
            dedupe_key=f"permission:{request_id}",
            source=self.source,
            title=str(record.get("summary") or "Autorisation requise"),
            body=str(record.get("tool_name") or ""),
            priority=100,
            action_required=True,
            kind="permission_request",
            urgency="critical",
            impact="high",
            occurred_at=parse_datetime(record.get("created_at")),
            expires_at=parse_datetime(record.get("expires_at")),
            metadata={
                "provider": self.source,
                "request_id": request_id,
                "tool_name": str(record.get("tool_name") or "Action"),
            },
        )

    def _usage_event(self, record: Mapping[str, Any]) -> Event:
        updated_at = parse_datetime(record.get("updated_at"))
        expires_at = parse_datetime(record.get("expires_at"))
        label = "Claude" if self.source == "claude" else "ChatGPT / Codex"
        return Event(
            id=f"{self.source}:usage",
            dedupe_key="usage",
            source=self.source,
            title=f"Utilisation {label}",
            priority=18,
            action_required=False,
            kind="agent_usage",
            urgency="low",
            impact="low",
            occurred_at=updated_at,
            expires_at=expires_at,
            metadata={
                "provider": self.source,
                "plan_type": str(record.get("plan_type") or ""),
                "windows": list(record.get("windows") or []),
            },
        )
