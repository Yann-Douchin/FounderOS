"""Concurrent connector scheduler with explicit source health."""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from founder_os.connectors.base import (
    Connector,
    ConnectorStaleError,
    ConnectorUnavailableError,
)
from founder_os.core.event_bus import EventBus
from founder_os.models import Event, utc_now


@dataclass(slots=True)
class ConnectorSchedule:
    connector: Connector
    next_poll_at: datetime
    future: Future[list[Event]] | None = None
    submitted_at: datetime | None = None
    started_monotonic: float = 0.0
    failures: int = 0
    last_error: str = ""
    last_event_count: int = 0
    last_success_at: datetime | None = None
    status: str = "starting"
    timed_out: bool = False
    owned_event_ids: set[str] = field(default_factory=set)


class Scheduler:
    def __init__(
        self,
        connectors: Iterable[Connector],
        bus: EventBus,
        logger: logging.Logger | None = None,
        *,
        max_workers: int = 4,
        force_wait_seconds: float = 30,
    ) -> None:
        epoch = datetime.min.replace(tzinfo=utc_now().tzinfo)
        self.schedules = [ConnectorSchedule(connector=item, next_poll_at=epoch) for item in connectors]
        self.bus = bus
        self.log = logger or logging.getLogger("founderos.scheduler")
        self.force_wait_seconds = max(1.0, float(force_wait_seconds))
        worker_count = max(1, min(int(max_workers), max(1, len(self.schedules))))
        self._executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="founderos-connector")
        self._closed = False
        started_at = utc_now()
        for schedule in self.schedules:
            self.bus.publish(self._health_event(schedule, started_at, status="starting"))

    def poll_due(self, now: datetime | None = None, *, force: bool = False) -> dict[str, int]:
        if self._closed:
            return {}
        now = now or utc_now()
        counts: dict[str, int] = {}
        self._harvest(now, counts)
        pending: list[Future[list[Event]]] = []
        for schedule in self.schedules:
            if schedule.future is not None:
                if force:
                    pending.append(schedule.future)
                continue
            if not force and schedule.next_poll_at > now:
                continue
            schedule.submitted_at = now
            schedule.started_monotonic = time.monotonic()
            schedule.timed_out = False
            schedule.status = "polling"
            schedule.next_poll_at = now + timedelta(seconds=schedule.connector.poll_interval_seconds)
            schedule.future = self._executor.submit(schedule.connector.poll, now)
            pending.append(schedule.future)
        if force and pending:
            wait(pending, timeout=self.force_wait_seconds)
        self._harvest(now, counts)
        self._mark_overdue(now)
        return counts

    def health_snapshot(self) -> dict[str, Mapping[str, Any]]:
        return {
            schedule.connector.name: {
                "status": schedule.status,
                "failures": schedule.failures,
                "last_error": schedule.last_error,
                "last_event_count": schedule.last_event_count,
                "last_success_at": schedule.last_success_at.isoformat() if schedule.last_success_at else None,
                "critical": schedule.connector.critical,
            }
            for schedule in self.schedules
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
        for schedule in self.schedules:
            try:
                schedule.connector.close()
            except Exception as exc:
                self.log.warning("connector %s close failed: %s", schedule.connector.name, _safe_error(exc))

    def _harvest(self, now: datetime, counts: dict[str, int]) -> None:
        for schedule in self.schedules:
            future = schedule.future
            if future is None or not future.done():
                continue
            schedule.future = None
            try:
                events = future.result()
                if not isinstance(events, list) or not all(isinstance(event, Event) for event in events):
                    raise TypeError("connector poll must return a list of Event objects")
            except Exception as exc:
                self._record_failure(schedule, exc, now)
                continue
            connector = schedule.connector
            if connector.emits_snapshot:
                incoming_ids = {event.id for event in events}
                self.bus.publish_many(events)
                for event_id in schedule.owned_event_ids - incoming_ids:
                    self.bus.remove(event_id)
                schedule.owned_event_ids = incoming_ids
            else:
                self.bus.publish_many(events)
            schedule.failures = 0
            schedule.last_error = ""
            schedule.last_event_count = len(events)
            schedule.last_success_at = now
            schedule.status = "healthy"
            schedule.timed_out = False
            schedule.next_poll_at = now + timedelta(seconds=connector.poll_interval_seconds)
            self.bus.remove(self._health_event_id(connector.name))
            counts[connector.name] = len(events)

    def _mark_overdue(self, now: datetime) -> None:
        current_monotonic = time.monotonic()
        for schedule in self.schedules:
            if schedule.future is None:
                continue
            if schedule.timed_out:
                self.bus.publish(self._health_event(schedule, now, status=schedule.status))
                continue
            elapsed = current_monotonic - schedule.started_monotonic
            if elapsed <= schedule.connector.poll_timeout_seconds:
                continue
            schedule.timed_out = True
            self._record_failure(
                schedule,
                TimeoutError(f"poll exceeded {schedule.connector.poll_timeout_seconds:.0f} seconds"),
                now,
                keep_future=True,
            )

    def _record_failure(
        self,
        schedule: ConnectorSchedule,
        error: Exception,
        now: datetime,
        *,
        keep_future: bool = False,
    ) -> None:
        if not keep_future:
            schedule.future = None
        schedule.failures += 1
        schedule.last_error = _safe_error(error)
        if isinstance(error, ConnectorStaleError):
            schedule.status = "stale"
        elif isinstance(error, ConnectorUnavailableError):
            schedule.status = "unavailable"
        elif isinstance(error, TimeoutError):
            schedule.status = "timeout"
        else:
            schedule.status = "degraded"
        connector = schedule.connector
        backoff = min(300.0, connector.poll_interval_seconds * (2 ** min(schedule.failures, 4)))
        schedule.next_poll_at = now + timedelta(seconds=backoff)
        self.bus.publish(self._health_event(schedule, now, status=schedule.status))
        self.log.warning(
            "connector %s is %s, retry in %.0fs: %s",
            connector.name,
            schedule.status,
            backoff,
            schedule.last_error,
        )

    def _health_event(self, schedule: ConnectorSchedule, now: datetime, *, status: str) -> Event:
        label = _source_label(schedule.connector.name)
        titles = {
            "starting": f"Connecting to {label}",
            "stale": f"Stale {label} data",
            "unavailable": f"{label} unavailable",
            "timeout": f"{label} timed out",
            "degraded": f"{label} degraded",
        }
        is_failure = status != "starting"
        return Event(
            id=self._health_event_id(schedule.connector.name),
            dedupe_key=f"connector-health:{schedule.connector.name}",
            source="founderos",
            title=titles.get(status, f"Unknown {label} status"),
            body=schedule.last_error,
            priority=(94 if schedule.connector.critical else 78) if is_failure else 38,
            action_required=is_failure,
            kind="connector_health",
            urgency="critical" if is_failure and schedule.connector.critical else "high" if is_failure else "low",
            impact="high" if schedule.connector.critical else "medium",
            occurred_at=now,
            expires_at=now + timedelta(days=1),
            metadata={
                "connector": schedule.connector.name,
                "status": status,
                "failures": schedule.failures,
                "critical": schedule.connector.critical,
            },
        )

    @staticmethod
    def _health_event_id(source: str) -> str:
        return f"founderos:connector-health:{source}"


def _safe_error(error: Exception) -> str:
    return " ".join(str(error).split())[:240] or type(error).__name__


def _source_label(source: str) -> str:
    return {
        "linear": "Linear",
        "slack": "Slack",
        "gmail": "Gmail",
        "calendar": "Calendar",
        "claude": "Claude",
        "chatgpt_codex": "Codex",
    }.get(source, source.replace("_", " ").title())
