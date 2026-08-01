"""Deterministic gallery scenarios that require no external credentials."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from founder_os.connectors.base import Connector
from founder_os.models import Event


SCENARIOS = ("mixed", "linear_blocker", "calendar", "gmail", "slack", "clear")


class DemoConnector(Connector):
    name = "demo"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.scenario = str(config.get("scenario", "mixed"))
        if self.scenario not in SCENARIOS:
            raise ValueError(f"unknown demo scenario: {self.scenario}")

    def poll(self, now: datetime) -> list[Event]:
        if self.scenario == "clear":
            return []
        events = {
            "linear_blocker": self._linear(now),
            "calendar": self._calendar(now),
            "gmail": self._gmail(now),
            "slack": self._slack(now),
        }
        if self.scenario == "mixed":
            return list(events.values())
        return [events[self.scenario]]

    @staticmethod
    def _linear(now: datetime) -> Event:
        return Event(
            id="demo:linear:qty-fix",
            source="linear",
            kind="blocker",
            title="QTY-142 Quantity Fix blocked",
            body="Waiting for founder decision",
            priority=90,
            action_required=True,
            urgency="critical",
            impact="high",
            occurred_at=now,
            expires_at=now + timedelta(hours=2),
            dedupe_key="demo:linear:qty-fix",
        )

    @staticmethod
    def _calendar(now: datetime) -> Event:
        start = now + timedelta(minutes=7)
        return Event(
            id="demo:calendar:investor",
            source="calendar",
            kind="meeting",
            title="Investor update in 7 min",
            priority=83,
            action_required=True,
            urgency="high",
            impact="high",
            occurred_at=now,
            due_at=start,
            expires_at=start + timedelta(hours=1),
            dedupe_key="demo:calendar:investor",
        )

    @staticmethod
    def _gmail(now: datetime) -> Event:
        return Event(
            id="demo:gmail:term-sheet",
            source="gmail",
            kind="email",
            title="Maya: Term sheet questions",
            priority=72,
            action_required=True,
            urgency="normal",
            impact="high",
            occurred_at=now,
            expires_at=now + timedelta(hours=8),
            dedupe_key="demo:gmail:term-sheet",
        )

    @staticmethod
    def _slack(now: datetime) -> Event:
        return Event(
            id="demo:slack:launch",
            source="slack",
            kind="message",
            title="#launch approval needed",
            priority=68,
            action_required=True,
            urgency="high",
            impact="medium",
            occurred_at=now,
            expires_at=now + timedelta(hours=2),
            dedupe_key="demo:slack:launch",
        )
