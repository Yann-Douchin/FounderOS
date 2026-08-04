"""Publish authoritative Google Calendar busy state to a BUSY Bar Matter switch."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Protocol

from founder_os.automation.occupancy import (
    MATTER_BUSY_STATES,
    OccupancyLeaseCoordinator,
    OccupancySnapshot,
)
from founder_os.display.busybar import BusyBarDisplay, DisplayError
from founder_os.models import Event, parse_datetime


class BusyStateTarget(Protocol):
    def synchronize(self, busy: bool) -> bool: ...


class BusyBarMatterTarget:
    """Use the BUSY Bar HTTP API to update its commissioned Matter switch."""

    def __init__(self, client: BusyBarDisplay, *, require_pairing: bool = True) -> None:
        self.client = client
        self.require_pairing = bool(require_pairing)

    def synchronize(self, busy: bool) -> bool:
        pairing = self.client.smart_home_pairing()
        try:
            fabric_count = int(pairing.get("fabric_count", 0))
        except (TypeError, ValueError) as exc:
            raise DisplayError("GET /api/smart_home/pairing returned an invalid fabric count") from exc
        if self.require_pairing and fabric_count < 1:
            raise DisplayError("BUSY Bar Matter has no commissioned smart-home fabric")
        current = self.client.smart_home_switch().get("state")
        if not isinstance(current, bool):
            raise DisplayError("GET /api/smart_home/switch returned an invalid state")
        if current != busy:
            self.client.set_smart_home_switch(busy, startup="off")
            verified = self.client.smart_home_switch().get("state")
            if verified != busy:
                raise DisplayError("BUSY Bar Matter switch did not confirm the requested state")
            current = verified
        return current


class CalendarBusyAutomation:
    """Non-blocking, fail-safe synchronization of current calendar occupancy."""

    name = "calendar_busy_indicator"

    def __init__(
        self,
        target: BusyStateTarget,
        *,
        include_all_day: bool = False,
        include_tentative: bool = True,
        off_delay_seconds: float = 15.0,
        verify_interval_seconds: float = 60.0,
        retry_seconds: float = 5.0,
        retry_max_seconds: float = 60.0,
        force_wait_seconds: float = 15.0,
        lease_min_ttl_seconds: float = 5.0,
        lease_max_ttl_seconds: float = 28_800.0,
        occupancy: OccupancyLeaseCoordinator | None = None,
    ) -> None:
        self.target = target
        self.include_all_day = bool(include_all_day)
        self.include_tentative = bool(include_tentative)
        self.off_delay = timedelta(seconds=max(0.0, float(off_delay_seconds)))
        self.verify_interval = timedelta(seconds=max(10.0, float(verify_interval_seconds)))
        self.retry_seconds = max(1.0, float(retry_seconds))
        self.retry_max_seconds = max(self.retry_seconds, float(retry_max_seconds))
        self.force_wait_seconds = max(1.0, float(force_wait_seconds))
        self.occupancy = occupancy or OccupancyLeaseCoordinator(
            min_ttl_seconds=lease_min_ttl_seconds,
            max_ttl_seconds=lease_max_ttl_seconds,
        )
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="founderos-automation")
        self._future: Future[bool] | None = None
        self._future_desired: bool | None = None
        self._desired_busy: bool | None = None
        self._desired_state: str | None = None
        self._desired_since: datetime | None = None
        self._applied_busy: bool | None = None
        self._last_success_at: datetime | None = None
        self._last_verified_at: datetime | None = None
        self._retry_at: datetime | None = None
        self._retry_count = 0
        self._target_error = ""
        self._calendar_error = ""
        self._active_event_count = 0
        self._presence = self.occupancy.snapshot()
        self._closed = False

    def reconcile(
        self,
        events: Iterable[Event],
        calendar_health: Mapping[str, Any] | None,
        now: datetime,
        *,
        wait: bool = False,
    ) -> Mapping[str, Any]:
        if self._closed:
            return self.snapshot(status="stopped")
        self._harvest(now)
        health = calendar_health or {}
        calendar_status = str(health.get("status") or "starting")
        has_snapshot = bool(health.get("last_success_at")) or calendar_status == "healthy"
        try:
            failures = int(health.get("failures", 0))
        except (TypeError, ValueError):
            failures = 1
        polling_is_clean = (
            calendar_status == "polling"
            and has_snapshot
            and failures == 0
            and not health.get("last_error")
        )
        authoritative = calendar_status == "healthy" or polling_is_clean
        if authoritative:
            self._calendar_error = ""
            active = calendar_busy_events(
                events,
                now,
                include_all_day=self.include_all_day,
                include_tentative=self.include_tentative,
            )
            self._active_event_count = len(active)
            self.occupancy.set_calendar_busy(bool(active))
        else:
            self._calendar_error = (
                "" if calendar_status in {"starting", "polling"}
                else f"Calendar source is {calendar_status}"
            )
        presence = self.occupancy.snapshot(now)
        previous_state = self._desired_state
        self._desired_state = presence.state
        self._presence = presence

        # An unknown Calendar source must not advertise availability. A local
        # busy lease may still turn the indicator on, and may later undo only
        # the busy state that it established itself.
        local_busy_was_applied = (
            previous_state in MATTER_BUSY_STATES - {"meeting"}
            and self._applied_busy is True
        )
        pending_owned_off = self._desired_busy is False and self._applied_busy is True
        desired_is_authoritative = (
            presence.calendar_known
            or presence.busy
            or local_busy_was_applied
            or pending_owned_off
        )
        if not desired_is_authoritative:
            return self.snapshot(status="starting" if not has_snapshot else "degraded")

        desired = presence.busy
        if desired != self._desired_busy:
            self._desired_busy = desired
            self._desired_since = now

        effective_desired = desired
        if (
            not desired
            and self._applied_busy is True
            and self._desired_since is not None
            and now - self._desired_since < self.off_delay
        ):
            effective_desired = True

        verification_due = authoritative and (
            self._last_verified_at is None
            or now - self._last_verified_at >= self.verify_interval
        )
        needs_sync = self._applied_busy != effective_desired or verification_due
        retry_ready = self._retry_at is None or now >= self._retry_at
        if self._future is None and needs_sync and retry_ready:
            self._future_desired = effective_desired
            self._future = self._executor.submit(self.target.synchronize, effective_desired)
        if wait and self._future is not None:
            try:
                self._future.result(timeout=self.force_wait_seconds)
            except FutureTimeout:
                self._target_error = "calendar busy indicator synchronization timed out"
            except Exception:
                # The completed worker is harvested below into bounded health
                # state. Target failures must never escape into the main loop.
                pass
            self._harvest(now)

        if self._target_error:
            status = "degraded"
        elif not authoritative:
            status = "starting" if not has_snapshot else "degraded"
        elif self._calendar_error:
            status = "degraded"
        elif self._last_success_at is None:
            status = "starting"
        else:
            status = "healthy"
        return self.snapshot(status=status)

    def snapshot(self, *, status: str) -> Mapping[str, Any]:
        return {
            "status": status,
            "critical": True,
            "desired_busy": self._desired_busy,
            "presence_state": self._presence.state,
            "applied_busy": self._applied_busy,
            "active_event_count": self._active_event_count,
            "active_lease_count": self._presence.active_lease_count,
            "calendar_known": self._presence.calendar_known,
            "last_success_at": self._last_success_at.isoformat() if self._last_success_at else None,
            "last_error": self._calendar_error or self._target_error,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _harvest(self, now: datetime) -> None:
        future = self._future
        if future is None or not future.done():
            return
        attempted = self._future_desired
        self._future = None
        self._future_desired = None
        try:
            applied = future.result()
            if not isinstance(applied, bool):
                raise DisplayError("calendar busy indicator returned an invalid state")
        except Exception as exc:
            self._retry_count += 1
            delay = min(
                self.retry_max_seconds,
                self.retry_seconds * (2 ** min(self._retry_count - 1, 8)),
            )
            self._retry_at = now + timedelta(seconds=delay)
            self._target_error = _safe_error(exc)
            return
        self._applied_busy = applied
        self._last_verified_at = now
        self._last_success_at = now
        self._retry_at = None
        self._retry_count = 0
        self._target_error = ""
        if attempted is not None and applied != attempted:
            self._target_error = "calendar busy indicator confirmed the wrong state"


def calendar_busy_events(
    events: Iterable[Event],
    now: datetime,
    *,
    include_all_day: bool,
    include_tentative: bool,
) -> list[Event]:
    active: list[Event] = []
    for event in events:
        if event.source != "calendar" or event.metadata.get("calendar_busy") is not True:
            continue
        if bool(event.metadata.get("all_day")) and not include_all_day:
            continue
        response = str(event.metadata.get("response_status") or "").strip().casefold()
        if response == "declined" or (response == "tentative" and not include_tentative):
            continue
        try:
            start = parse_datetime(event.metadata.get("start_at"))
            end = parse_datetime(event.metadata.get("end_at"))
        except ValueError:
            continue
        if start is not None and end is not None and start <= now < end:
            active.append(event)
    return active


def _safe_error(error: Exception) -> str:
    return " ".join(str(error).split())[:240] or type(error).__name__
