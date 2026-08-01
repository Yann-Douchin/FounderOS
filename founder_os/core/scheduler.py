"""Polling scheduler that turns connector snapshots into bus events."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from founder_os.connectors.base import Connector, ConnectorError
from founder_os.core.event_bus import EventBus
from founder_os.models import utc_now


@dataclass(slots=True)
class ConnectorSchedule:
    connector: Connector
    next_poll_at: datetime
    failures: int = 0
    last_error: str = ""
    last_event_count: int = 0


class Scheduler:
    def __init__(self, connectors: Iterable[Connector], bus: EventBus, logger: logging.Logger | None = None) -> None:
        epoch = datetime.min.replace(tzinfo=utc_now().tzinfo)
        self.schedules = [ConnectorSchedule(connector=item, next_poll_at=epoch) for item in connectors]
        self.bus = bus
        self.log = logger or logging.getLogger("founderos.scheduler")

    def poll_due(self, now: datetime | None = None, *, force: bool = False) -> dict[str, int]:
        now = now or utc_now()
        counts: dict[str, int] = {}
        for schedule in self.schedules:
            if not force and schedule.next_poll_at > now:
                continue
            connector = schedule.connector
            try:
                events = connector.poll(now)
                if connector.emits_snapshot:
                    self.bus.reconcile_source(connector.name, events)
                else:
                    self.bus.publish_many(events)
                schedule.failures = 0
                schedule.last_error = ""
                schedule.last_event_count = len(events)
                schedule.next_poll_at = now + timedelta(seconds=connector.poll_interval_seconds)
                counts[connector.name] = len(events)
            except (ConnectorError, OSError, ValueError) as exc:
                schedule.failures += 1
                schedule.last_error = str(exc)
                backoff = min(300.0, connector.poll_interval_seconds * (2 ** min(schedule.failures, 4)))
                schedule.next_poll_at = now + timedelta(seconds=backoff)
                self.log.warning("connector %s failed, retry in %.0fs: %s", connector.name, backoff, exc)
        return counts

    def close(self) -> None:
        for schedule in self.schedules:
            schedule.connector.close()
