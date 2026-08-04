"""Thread-safe, expiring occupancy leases for local workspace integrations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import Final

from founder_os.models import UTC, utc_now


PRESENCE_PRIORITY: Final[dict[str, int]] = {
    "available": 0,
    "focus": 1,
    "manual_call": 2,
    "meeting": 3,
    "recording": 4,
}
EXTERNAL_PRESENCE_STATES: Final[frozenset[str]] = frozenset(
    {"focus", "manual_call", "recording"}
)
MATTER_BUSY_STATES: Final[frozenset[str]] = frozenset(
    {"manual_call", "meeting", "recording"}
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}")


@dataclass(frozen=True, slots=True)
class OccupancyLease:
    """One bounded local claim over the shared presence state."""

    lease_id: str
    source: str
    state: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OccupancySnapshot:
    """Content-free aggregate suitable for health and local control surfaces."""

    state: str
    busy: bool
    calendar_known: bool
    calendar_busy: bool
    active_lease_count: int
    next_expiry_at: datetime | None

    def public_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "busy": self.busy,
            "calendar_known": self.calendar_known,
            "active_lease_count": self.active_lease_count,
            "next_expiry_at": self.next_expiry_at.isoformat() if self.next_expiry_at else None,
        }


class OccupancyLeaseCoordinator:
    """Aggregate Calendar and authenticated local leases deterministically.

    Calendar is represented separately from expiring local leases. This preserves
    the established fail-safe rule: stale Calendar health holds its last known
    state instead of silently advertising availability.
    """

    def __init__(
        self,
        *,
        min_ttl_seconds: float = 5.0,
        max_ttl_seconds: float = 28_800.0,
    ) -> None:
        self.min_ttl_seconds = max(1.0, float(min_ttl_seconds))
        self.max_ttl_seconds = max(self.min_ttl_seconds, float(max_ttl_seconds))
        self._lock = RLock()
        self._leases: dict[str, OccupancyLease] = {}
        self._calendar_known = False
        self._calendar_busy = False

    def set_calendar_busy(self, busy: bool) -> None:
        """Commit one authoritative Calendar observation."""
        with self._lock:
            self._calendar_known = True
            self._calendar_busy = bool(busy)

    def acquire(
        self,
        lease_id: str,
        state: str,
        ttl_seconds: float,
        *,
        source: str,
        now: datetime | None = None,
    ) -> OccupancyLease:
        lease_id = _validated_identifier(lease_id, "lease_id")
        source = _validated_identifier(source, "source")
        state = str(state).strip().lower()
        if state not in EXTERNAL_PRESENCE_STATES:
            raise ValueError("state is not available to external lease holders")
        ttl = self._validated_ttl(ttl_seconds)
        timestamp = _aware_utc(now or utc_now())
        with self._lock:
            self._prune(timestamp)
            existing = self._leases.get(lease_id)
            if existing and (existing.source != source or existing.state != state):
                raise ValueError("lease_id is already held with a different owner or state")
            issued_at = existing.issued_at if existing else timestamp
            lease = OccupancyLease(
                lease_id=lease_id,
                source=source,
                state=state,
                issued_at=issued_at,
                expires_at=timestamp + timedelta(seconds=ttl),
            )
            self._leases[lease_id] = lease
            return lease

    def renew(
        self,
        lease_id: str,
        ttl_seconds: float,
        *,
        source: str,
        now: datetime | None = None,
    ) -> OccupancyLease:
        lease_id = _validated_identifier(lease_id, "lease_id")
        source = _validated_identifier(source, "source")
        ttl = self._validated_ttl(ttl_seconds)
        timestamp = _aware_utc(now or utc_now())
        with self._lock:
            self._prune(timestamp)
            existing = self._leases.get(lease_id)
            if existing is None or existing.source != source:
                raise ValueError("lease is absent, expired, or owned by another source")
            lease = OccupancyLease(
                lease_id=existing.lease_id,
                source=existing.source,
                state=existing.state,
                issued_at=existing.issued_at,
                expires_at=timestamp + timedelta(seconds=ttl),
            )
            self._leases[lease_id] = lease
            return lease

    def release(
        self,
        lease_id: str,
        *,
        source: str,
        now: datetime | None = None,
    ) -> bool:
        lease_id = _validated_identifier(lease_id, "lease_id")
        source = _validated_identifier(source, "source")
        timestamp = _aware_utc(now or utc_now())
        with self._lock:
            self._prune(timestamp)
            existing = self._leases.get(lease_id)
            if existing is None:
                return False
            if existing.source != source:
                raise ValueError("lease is owned by another source")
            del self._leases[lease_id]
            return True

    def release_all(self, *, source: str, now: datetime | None = None) -> int:
        """Release only leases owned by one authenticated local source."""
        source = _validated_identifier(source, "source")
        timestamp = _aware_utc(now or utc_now())
        with self._lock:
            self._prune(timestamp)
            owned = [
                lease_id
                for lease_id, lease in self._leases.items()
                if lease.source == source
            ]
            for lease_id in owned:
                del self._leases[lease_id]
            return len(owned)

    def snapshot(self, now: datetime | None = None) -> OccupancySnapshot:
        timestamp = _aware_utc(now or utc_now())
        with self._lock:
            self._prune(timestamp)
            states = [lease.state for lease in self._leases.values()]
            if self._calendar_known and self._calendar_busy:
                states.append("meeting")
            state = max(states, key=PRESENCE_PRIORITY.__getitem__) if states else "available"
            next_expiry = min(
                (lease.expires_at for lease in self._leases.values()),
                default=None,
            )
            return OccupancySnapshot(
                state=state,
                busy=state in MATTER_BUSY_STATES,
                calendar_known=self._calendar_known,
                calendar_busy=self._calendar_busy,
                active_lease_count=len(self._leases),
                next_expiry_at=next_expiry,
            )

    def _validated_ttl(self, value: float) -> float:
        if isinstance(value, bool):
            raise ValueError("ttl_seconds must be numeric")
        try:
            ttl = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("ttl_seconds must be numeric") from exc
        if not self.min_ttl_seconds <= ttl <= self.max_ttl_seconds:
            raise ValueError(
                f"ttl_seconds must be between {self.min_ttl_seconds:g} and "
                f"{self.max_ttl_seconds:g}"
            )
        return ttl

    def _prune(self, now: datetime) -> None:
        self._leases = {
            lease_id: lease
            for lease_id, lease in self._leases.items()
            if lease.expires_at > now
        }


def _validated_identifier(value: str, field: str) -> str:
    normalized = str(value).strip()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{field} must contain 1 to 96 safe identifier characters")
    return normalized


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("lease timestamps must include a timezone")
    return value.astimezone(UTC)
